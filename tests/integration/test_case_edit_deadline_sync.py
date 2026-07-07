from __future__ import annotations

import uuid
from datetime import date, timedelta


def _latest_docket(matter_id: str, name_ref: str):
    from app.models.docket import DocketItem

    return (
        DocketItem.query.filter_by(matter_id=str(matter_id), name_ref=name_ref)
        .order_by(DocketItem.docket_id.desc())
        .first()
    )


def test_edit_matter_syncs_core_deadlines_and_clears_removed_exam_deadline(
    admin_client, db_session
):
    from app.models.ip_records import Matter, MatterCustomField, VMatterOverview

    matter_id = uuid.uuid4().hex
    our_ref = f"26PD{uuid.uuid4().hex[:4].upper()}US"
    filing_deadline = (date.today() + timedelta(days=30)).isoformat()
    exam_deadline = (date.today() + timedelta(days=60)).isoformat()
    reg_deadline = (date.today() + timedelta(days=90)).isoformat()

    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Text Text",
            right_group="DOM",
            matter_type="PATENT",
            retained_at="2026-01-01",
            entered_at="2026-01-01",
            status_red="",
            status_red_related_date="",
            status_blue="",
            is_deleted=False,
        )
    )
    db_session.add(
        VMatterOverview(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Text Text",
            right_group="DOM",
            matter_type="PATENT",
            applicants="",
            clients="",
            attorneys="",
            entered_at="2026-01-01",
        )
    )
    db_session.add(MatterCustomField(matter_id=matter_id, namespace="domestic_patent", data={}))
    db_session.commit()

    resp = admin_client.post(
        f"/case/matter/{matter_id}/edit",
        data={
            "idempotency_key": uuid.uuid4().hex,
            "our_ref": our_ref,
            "old_our_ref": "",
            "your_ref": "",
            "right_name": "Text Text",
            "inhouse_status": "",
            "retained_at": "2026-01-01",
            "entered_at": "2026-01-01",
            "memo": "",
            "filing_deadline": filing_deadline,
            "exam_deadline": exam_deadline,
            "reg_deadline": reg_deadline,
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    db_session.expire_all()
    filing = _latest_docket(matter_id, "Filing")
    exam = _latest_docket(matter_id, "Examination request")
    reg = _latest_docket(matter_id, "Registration")
    mgmt_exam = _latest_docket(matter_id, "MGMT:STATUS_RED:Examination requestDeadline")

    assert filing is not None
    assert filing.due_date == filing_deadline
    assert not (filing.done_date or "").strip()

    assert exam is not None
    assert exam.due_date == exam_deadline
    assert not (exam.done_date or "").strip()

    assert reg is not None
    assert reg.due_date == reg_deadline
    assert not (reg.done_date or "").strip()

    assert mgmt_exam is not None
    assert mgmt_exam.due_date == exam_deadline
    assert not (mgmt_exam.done_date or "").strip()

    resp2 = admin_client.post(
        f"/case/matter/{matter_id}/edit",
        data={
            "idempotency_key": uuid.uuid4().hex,
            "our_ref": our_ref,
            "old_our_ref": "",
            "your_ref": "",
            "right_name": "Text Text",
            "inhouse_status": "",
            "retained_at": "2026-01-01",
            "entered_at": "2026-01-01",
            "memo": "",
            "filing_deadline": filing_deadline,
            "exam_deadline": "",
            "reg_deadline": reg_deadline,
        },
        follow_redirects=False,
    )
    assert resp2.status_code in (302, 303)

    db_session.expire_all()
    exam_after_clear = _latest_docket(matter_id, "Examination request")
    mgmt_exam_after_clear = _latest_docket(
        matter_id, "MGMT:STATUS_RED:Examination requestDeadline"
    )

    assert exam_after_clear is not None
    assert (exam_after_clear.done_date or "").startswith("AUTO_CANCELLED:")

    assert mgmt_exam_after_clear is not None
    assert mgmt_exam_after_clear.due_date == exam_deadline
    assert not (mgmt_exam_after_clear.done_date or "").strip()


def test_edit_matter_filing_deadline_type_internal_updates_internal_due(admin_client, db_session):
    from app.models.ip_records import Matter, MatterCustomField, VMatterOverview

    matter_id = uuid.uuid4().hex
    our_ref = f"26PD{uuid.uuid4().hex[:4].upper()}US"

    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Text Text Text",
            right_group="DOM",
            matter_type="PATENT",
            retained_at="2026-01-01",
            entered_at="2026-01-01",
            status_red="",
            status_red_related_date="",
            status_blue="",
            is_deleted=False,
        )
    )
    db_session.add(
        VMatterOverview(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Text Text Text",
            right_group="DOM",
            matter_type="PATENT",
            applicants="",
            clients="",
            attorneys="",
            entered_at="2026-01-01",
        )
    )
    db_session.add(MatterCustomField(matter_id=matter_id, namespace="domestic_patent", data={}))
    db_session.commit()

    resp = admin_client.post(
        f"/case/matter/{matter_id}/edit",
        data={
            "idempotency_key": uuid.uuid4().hex,
            "our_ref": our_ref,
            "old_our_ref": "",
            "your_ref": "",
            "right_name": "Text Text Text",
            "inhouse_status": "",
            "retained_at": "2026-01-01",
            "entered_at": "2026-01-01",
            "memo": "",
            "filing_deadline": "2026-03-21",
            "filing_deadline_type": "INTERNAL",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    db_session.expire_all()
    filing = _latest_docket(matter_id, "Filing")
    assert filing is not None
    assert (filing.due_date or "") == ""
    assert filing.extended_due_date == "2026-03-21"
    assert not (filing.done_date or "").strip()

    resp2 = admin_client.post(
        f"/case/matter/{matter_id}/edit",
        data={
            "idempotency_key": uuid.uuid4().hex,
            "our_ref": our_ref,
            "old_our_ref": "",
            "your_ref": "",
            "right_name": "Text Text Text",
            "inhouse_status": "",
            "retained_at": "2026-01-01",
            "entered_at": "2026-01-01",
            "memo": "",
            "filing_deadline": "2026-03-21",
            "filing_deadline_type": "LEGAL",
        },
        follow_redirects=False,
    )
    assert resp2.status_code in (302, 303)

    db_session.expire_all()
    filing_legal = _latest_docket(matter_id, "Filing")
    assert filing_legal is not None
    assert filing_legal.due_date == "2026-03-21"
    assert (filing_legal.extended_due_date or "") == ""
    assert not (filing_legal.done_date or "").strip()


def test_edit_matter_defaults_exam_request_date_when_exam_requested_yes(admin_client, db_session):
    from app.models.ip_records import Matter, MatterCustomField, VMatterOverview

    matter_id = uuid.uuid4().hex
    our_ref = f"26PD{uuid.uuid4().hex[:4].upper()}US"
    app_date = "2026-02-12"

    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Text Text Text",
            right_group="DOM",
            matter_type="PATENT",
            retained_at="2026-01-01",
            entered_at="2026-01-01",
            status_red="",
            status_red_related_date="",
            status_blue="",
            is_deleted=False,
        )
    )
    db_session.add(
        VMatterOverview(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Text Text Text",
            right_group="DOM",
            matter_type="PATENT",
            applicants="",
            clients="",
            attorneys="",
            entered_at="2026-01-01",
        )
    )
    db_session.add(MatterCustomField(matter_id=matter_id, namespace="domestic_patent", data={}))
    db_session.commit()

    resp = admin_client.post(
        f"/case/matter/{matter_id}/edit",
        data={
            "idempotency_key": uuid.uuid4().hex,
            "our_ref": our_ref,
            "old_our_ref": "",
            "your_ref": "",
            "right_name": "Text Text Text",
            "inhouse_status": "",
            "retained_at": "2026-01-01",
            "entered_at": "2026-01-01",
            "memo": "",
            "application_date": app_date,
            "exam_requested": "Yes",
            "exam_request_date": "",
            "exam_deadline": "2026-04-01",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    db_session.expire_all()
    row = MatterCustomField.query.filter_by(
        matter_id=matter_id, namespace="domestic_patent"
    ).first()
    assert row is not None
    data = row.data or {}
    assert data.get("exam_request_date") == app_date

    exam = _latest_docket(matter_id, "Examination request")
    assert exam is not None
    assert exam.done_date == app_date


def test_edit_matter_does_not_auto_cancel_filing_when_deadline_cleared(admin_client, db_session):
    from app.models.ip_records import Matter, MatterCustomField, VMatterOverview

    matter_id = uuid.uuid4().hex
    our_ref = f"26PD{uuid.uuid4().hex[:4].upper()}US"

    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Text Text Text Text",
            right_group="DOM",
            matter_type="PATENT",
            retained_at="2026-01-01",
            entered_at="2026-01-01",
            status_red="",
            status_red_related_date="",
            status_blue="",
            is_deleted=False,
        )
    )
    db_session.add(
        VMatterOverview(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Text Text Text Text",
            right_group="DOM",
            matter_type="PATENT",
            applicants="",
            clients="",
            attorneys="",
            entered_at="2026-01-01",
        )
    )
    db_session.add(MatterCustomField(matter_id=matter_id, namespace="domestic_patent", data={}))
    db_session.commit()

    resp = admin_client.post(
        f"/case/matter/{matter_id}/edit",
        data={
            "idempotency_key": uuid.uuid4().hex,
            "our_ref": our_ref,
            "old_our_ref": "",
            "your_ref": "",
            "right_name": "Text Text Text Text",
            "inhouse_status": "",
            "retained_at": "2026-01-01",
            "entered_at": "2026-01-01",
            "memo": "",
            "filing_deadline": "2026-03-15",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    db_session.expire_all()
    filing_before_clear = _latest_docket(matter_id, "Filing")
    assert filing_before_clear is not None
    assert filing_before_clear.due_date == "2026-03-15"
    assert not (filing_before_clear.done_date or "").strip()

    resp2 = admin_client.post(
        f"/case/matter/{matter_id}/edit",
        data={
            "idempotency_key": uuid.uuid4().hex,
            "our_ref": our_ref,
            "old_our_ref": "",
            "your_ref": "",
            "right_name": "Text Text Text Text",
            "inhouse_status": "",
            "retained_at": "2026-01-01",
            "entered_at": "2026-01-01",
            "memo": "",
            "filing_deadline": "",
        },
        follow_redirects=False,
    )
    assert resp2.status_code in (302, 303)

    db_session.expire_all()
    filing_after_clear = _latest_docket(matter_id, "Filing")
    assert filing_after_clear is not None
    assert filing_after_clear.due_date == "2026-03-15"
    assert not (filing_after_clear.done_date or "").strip()


def test_edit_matter_creates_priority_exam_progress_status_red_after_application(
    admin_client, db_session
):
    from app.models.ip_records import Matter, MatterCustomField, VMatterOverview

    matter_id = uuid.uuid4().hex
    our_ref = f"26PD{uuid.uuid4().hex[:4].upper()}US"

    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Text Text",
            right_group="DOM",
            matter_type="PATENT",
            retained_at="2026-01-01",
            entered_at="2026-01-01",
            status_red="",
            status_red_related_date="",
            status_blue="",
            is_deleted=False,
        )
    )
    db_session.add(
        VMatterOverview(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Text Text",
            right_group="DOM",
            matter_type="PATENT",
            applicants="",
            clients="",
            attorneys="",
            entered_at="2026-01-01",
        )
    )
    db_session.add(MatterCustomField(matter_id=matter_id, namespace="domestic_patent", data={}))
    db_session.commit()

    resp = admin_client.post(
        f"/case/matter/{matter_id}/edit",
        data={
            "idempotency_key": uuid.uuid4().hex,
            "our_ref": our_ref,
            "old_our_ref": "",
            "your_ref": "",
            "right_name": "Text Text",
            "inhouse_status": "",
            "retained_at": "2026-01-01",
            "entered_at": "2026-01-01",
            "memo": "",
            "priority_exam_request": "Yes",
            "application_date": "2026-04-11",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    db_session.expire_all()
    priority_row = _latest_docket(matter_id, "MGMT:STATUS_RED:ExaminationOpen")
    matter = Matter.query.get(matter_id)

    assert priority_row is not None
    assert priority_row.due_date == "2026-04-18"
    assert not (priority_row.done_date or "").strip()

    assert matter is not None
    assert matter.status_red == "ExaminationOpen"
    assert matter.status_red_related_date == "2026-04-18"


def test_edit_matter_refreshes_stale_term_expiry_status_red_date_for_domestic_trademark(
    admin_client, db_session
):
    from app.models.ip_records import Matter, MatterCustomField, MatterEvent, VMatterOverview

    matter_id = uuid.uuid4().hex
    our_ref = f"25TD{uuid.uuid4().hex[:4].upper()}US"

    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Text Text Text Text",
            right_group="DOM",
            matter_type="TRADEMARK",
            retained_at="2025-01-01",
            entered_at="2025-01-01",
            status_red="Term expired",
            status_red_related_date="2035-05-09",
            status_blue="Text",
            is_deleted=False,
        )
    )
    db_session.add(
        VMatterOverview(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Text Text Text Text",
            right_group="DOM",
            matter_type="TRADEMARK",
            applicants="",
            clients="",
            attorneys="",
            entered_at="2025-01-01",
        )
    )
    db_session.add(
        MatterCustomField(
            matter_id=matter_id,
            namespace="domestic_trademark",
            data={
                "registration_date": "2025-05-09",
                "term_expiry_date": "2035-05-09",
            },
        )
    )
    db_session.add(
        MatterEvent(
            matter_id=matter_id,
            event_key=" Period ",
            event_at="2035-05-09",
            source_column="form:domestic_trademark",
        )
    )
    db_session.commit()

    resp = admin_client.post(
        f"/case/matter/{matter_id}/edit",
        data={
            "idempotency_key": uuid.uuid4().hex,
            "our_ref": our_ref,
            "old_our_ref": "",
            "your_ref": "",
            "right_name": "Text Text Text Text",
            "inhouse_status": "",
            "retained_at": "2025-01-01",
            "entered_at": "2025-01-01",
            "memo": "",
            "registration_date": "2025-05-09",
            "term_expiry_date": "2030-05-09",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    db_session.expire_all()
    matter = Matter.query.get(matter_id)
    event = MatterEvent.query.filter_by(
        matter_id=matter_id,
        event_key=" Period ",
        source_column="form:domestic_trademark",
    ).first()

    assert matter is not None
    assert matter.status_red == "Term expired"
    assert matter.status_red_related_date == "2030-05-09"
    assert event is not None
    assert event.event_at == "2030-05-09"
