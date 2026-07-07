from __future__ import annotations

import json
import uuid
from datetime import date, timedelta


def _create_patent_matter(
    db_session,
    *,
    matter_type: str = "PATENT",
    right_group: str = "DOM",
    custom_data: dict | None = None,
    status_red: str = "",
    status_red_related_date: str = "",
) -> str:
    from app.models.ip_records import Matter, MatterCustomField

    matter_id = uuid.uuid4().hex
    if (matter_type or "").strip().upper() == "PCT":
        our_ref = f"PCT/US{uuid.uuid4().hex[:8].upper()}"
    else:
        our_ref = f"26PD{uuid.uuid4().hex[:4].upper()}US"
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Text Text Text",
            right_group=right_group,
            matter_type=matter_type,
            retained_at="2026-01-01",
            entered_at="2026-01-01",
            status_red=status_red,
            status_red_related_date=status_red_related_date,
            status_blue="",
            is_deleted=False,
        )
    )
    db_session.add(
        MatterCustomField(
            matter_id=matter_id,
            namespace="domestic_patent",
            data=dict(custom_data or {}),
        )
    )
    db_session.commit()
    return matter_id


def _load_dockets(db_session, *, matter_id: str, name_ref: str) -> list:
    from app.models.docket import DocketItem

    return (
        DocketItem.query.filter_by(matter_id=str(matter_id), name_ref=name_ref)
        .order_by(DocketItem.docket_id.asc())
        .all()
    )


def test_foreign_notice_3m_uses_manual_due_when_engine_missing(db_session, monkeypatch):
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        custom_data={"foreign_filing_deadline": "2026-12-15"},
    )
    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    rows = _load_dockets(db_session, matter_id=matter_id, name_ref="MGMT:FOREIGN_FILING_NOTICE_3M")
    assert len(rows) == 1
    notice = rows[0]
    assert notice.due_date == "2026-09-15"
    # Visible 2 weeks before the notice due date
    assert notice.visible_from_date == "2026-09-01"
    memo = json.loads(notice.memo or "{}")
    assert memo.get("base_due") == "2026-12-15"
    assert memo.get("base_due_source") == "custom:foreign_filing_deadline"


def test_foreign_notice_3m_uses_status_red_related_date_fallback(db_session, monkeypatch):
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        custom_data={},
        status_red="ForeignFilingDeadline",
        status_red_related_date="2026-11-30",
    )
    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    rows = _load_dockets(db_session, matter_id=matter_id, name_ref="MGMT:FOREIGN_FILING_NOTICE_3M")
    assert len(rows) == 1
    notice = rows[0]
    assert notice.due_date == "2026-08-30"
    # Visible 2 weeks before the notice due date
    assert notice.visible_from_date == "2026-08-16"
    memo = json.loads(notice.memo or "{}")
    assert memo.get("base_due") == "2026-11-30"
    assert memo.get("base_due_source") == "matter_status_red_related_date"


def test_foreign_notice_3m_marks_done_when_foreign_filing_date_exists(db_session, monkeypatch):
    from app.models.ip_records import MatterCustomField
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        custom_data={"foreign_filing_deadline": "2026-12-15"},
    )
    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    row = MatterCustomField.query.filter_by(
        matter_id=matter_id, namespace="domestic_patent"
    ).first()
    assert row is not None
    data = dict(row.data or {})
    data["foreign_filing_date"] = "2026-10-01"
    row.data = data
    db_session.add(row)
    db_session.commit()

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    rows = _load_dockets(db_session, matter_id=matter_id, name_ref="MGMT:FOREIGN_FILING_NOTICE_3M")
    assert len(rows) == 1
    notice = rows[0]
    assert notice.done_date == "2026-10-01"


def test_foreign_filing_status_red_visible_from_is_1m_before_due(db_session, monkeypatch):
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        custom_data={"foreign_filing_deadline": "2026-12-15"},
    )
    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    rows = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:STATUS_RED:ForeignFilingDeadline",
    )
    assert len(rows) == 1
    deadline = rows[0]
    assert deadline.due_date == "2026-12-15"
    # Visible 1 month before the legal due date
    assert deadline.visible_from_date == "2026-11-15"


def test_incoming_pct_national_phase_excludes_foreign_filing_deadlines(db_session, monkeypatch):
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        right_group="INC",
        matter_type="PATENT",
        custom_data={
            "app_route": "PCT",
            "pct_application_no": "PCT/US2024/011704",
            "foreign_filing_deadline": "2026-12-15",
            "priority_date": "2026-01-15",
            "exam_deadline": "2028-01-15",
        },
    )
    monkeypatch.setattr(
        mgmt_deadlines,
        "_compute_engine_deadlines",
        lambda **kwargs: {"FOREIGN_FILING_PARIS": [date(2026, 12, 15)]},
    )
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    foreign_rows = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:STATUS_RED:ForeignFilingDeadline",
    )
    notice_rows = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:FOREIGN_FILING_NOTICE_3M",
    )
    exam_rows = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:STATUS_RED:Examination requestDeadline",
    )

    assert foreign_rows == []
    assert notice_rows == []
    assert len(exam_rows) == 1
    assert exam_rows[0].due_date == "2028-01-15"


def test_incoming_pct_national_phase_cancels_existing_foreign_filing_deadlines(
    db_session, monkeypatch
):
    from app.models.docket import DocketItem
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        right_group="INC",
        matter_type="PATENT",
        custom_data={
            "app_route": "PCT",
            "foreign_filing_deadline": "2026-12-15",
            "exam_deadline": "2028-01-15",
        },
        status_red="ForeignFilingDeadline",
        status_red_related_date="2026-12-15",
    )
    db_session.add(
        DocketItem(
            docket_id=uuid.uuid4().hex,
            matter_id=str(matter_id),
            category="MGMT_WORK",
            name_ref="MGMT:STATUS_RED:ForeignFilingDeadline",
            name_free="ForeignFilingDeadline",
            due_date="2026-12-15",
            done_date=None,
            memo=json.dumps({"auto": True, "trigger": "core_deadline"}, ensure_ascii=False),
            is_deleted=False,
        )
    )
    db_session.add(
        DocketItem(
            docket_id=uuid.uuid4().hex,
            matter_id=str(matter_id),
            category="MGMT",
            name_ref="MGMT:FOREIGN_FILING_NOTICE_3M",
            name_free="Notice to client (3 days)",
            due_date="2026-09-15",
            done_date=None,
            memo=json.dumps({"auto": True, "trigger": "deadline_code"}, ensure_ascii=False),
            is_deleted=False,
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        mgmt_deadlines,
        "_compute_engine_deadlines",
        lambda **kwargs: {"FOREIGN_FILING_PARIS": [date(2026, 12, 15)]},
    )
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    foreign_rows = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:STATUS_RED:ForeignFilingDeadline",
    )
    notice_rows = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:FOREIGN_FILING_NOTICE_3M",
    )

    assert len(foreign_rows) == 1
    assert foreign_rows[0].done_date == f"AUTO_CANCELLED:{date.today().isoformat()}"
    assert json.loads(foreign_rows[0].memo or "{}").get("close_reason") == (
        "excluded_pct_national_phase"
    )
    assert len(notice_rows) == 1
    assert notice_rows[0].done_date == f"AUTO_CANCELLED:{date.today().isoformat()}"


def test_pct_application_excludes_paris_foreign_filing_deadlines(db_session, monkeypatch):
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        matter_type="PCT",
        right_group="OUT",
        custom_data={
            "foreign_filing_deadline": "2026-12-15",
            "priority_date": "2026-01-15",
            "national_phase_deadline": "2028-07-15",
        },
    )
    monkeypatch.setattr(
        mgmt_deadlines,
        "_compute_engine_deadlines",
        lambda **kwargs: {
            "FOREIGN_FILING_PARIS": [date(2026, 12, 15)],
            "PCT_NATIONAL_PHASE": [date(2028, 7, 15)],
        },
    )
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    assert (
        _load_dockets(
            db_session,
            matter_id=matter_id,
            name_ref="MGMT:STATUS_RED:ForeignFilingDeadline",
        )
        == []
    )
    assert (
        _load_dockets(
            db_session,
            matter_id=matter_id,
            name_ref="MGMT:FOREIGN_FILING_NOTICE_3M",
        )
        == []
    )
    pct_rows = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:STATUS_RED:PCTDomesticDeadline",
    )
    assert len(pct_rows) == 1
    assert pct_rows[0].due_date == "2028-07-15"


def test_pct_current_legacy_19m_status_red_is_closed_not_reopened(db_session, monkeypatch):
    from app.models.docket import DocketItem
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        matter_type="PCT",
        right_group="ETC",
        custom_data={"national_phase_19m_deadline": "2027-08-30"},
        status_red="PCTPreliminary examinationDeadline",
        status_red_related_date="2027-08-30",
    )
    db_session.add(
        DocketItem(
            docket_id=uuid.uuid4().hex,
            matter_id=str(matter_id),
            category="MGMT",
            name_ref="MGMT:STATUS_RED:PCTPreliminary examinationDeadline",
            name_free="PCTPreliminary examinationDeadline",
            due_date="2027-08-30",
            done_date=None,
            memo=json.dumps({"auto": True, "trigger": "status_red"}, ensure_ascii=False),
            is_deleted=False,
        )
    )
    db_session.commit()

    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    rows = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:STATUS_RED:PCTPreliminary examinationDeadline",
    )
    assert len(rows) == 1
    assert rows[0].done_date == date.today().isoformat()
    assert json.loads(rows[0].memo or "{}").get("close_reason") == (
        "superseded_pct_advisory_status_red"
    )


def test_protocol_route_excludes_paris_foreign_filing_deadlines(db_session, monkeypatch):
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        matter_type="TRADEMARK",
        right_group="INC",
        custom_data={
            "app_route": "PCT",
            "foreign_filing_deadline": "2026-12-15",
            "priority_date": "2026-01-15",
        },
    )
    monkeypatch.setattr(
        mgmt_deadlines,
        "_compute_engine_deadlines",
        lambda **kwargs: {"FOREIGN_FILING_PARIS": [date(2026, 12, 15)]},
    )
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    assert (
        _load_dockets(
            db_session,
            matter_id=matter_id,
            name_ref="MGMT:STATUS_RED:ForeignFilingDeadline",
        )
        == []
    )
    assert (
        _load_dockets(
            db_session,
            matter_id=matter_id,
            name_ref="MGMT:FOREIGN_FILING_NOTICE_3M",
        )
        == []
    )


def test_outgoing_application_date_completes_foreign_filing_deadlines(db_session, monkeypatch):
    from app.models.docket import DocketItem
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        right_group="OUT",
        matter_type="PATENT",
        custom_data={
            "application_date": "2026-02-01",
            "foreign_filing_deadline": "2026-12-15",
            "priority_date": "2026-01-15",
        },
    )
    db_session.add(
        DocketItem(
            docket_id=uuid.uuid4().hex,
            matter_id=str(matter_id),
            category="MGMT_WORK",
            name_ref="MGMT:STATUS_RED:ForeignFilingDeadline",
            name_free="ForeignFilingDeadline",
            due_date="2026-12-15",
            done_date=None,
            memo=json.dumps({"auto": True, "trigger": "core_deadline"}, ensure_ascii=False),
            is_deleted=False,
        )
    )
    db_session.add(
        DocketItem(
            docket_id=uuid.uuid4().hex,
            matter_id=str(matter_id),
            category="MGMT",
            name_ref="MGMT:FOREIGN_FILING_NOTICE_3M",
            name_free="Notice to client (3 days)",
            due_date="2026-09-15",
            done_date=None,
            memo=json.dumps({"auto": True, "trigger": "deadline_code"}, ensure_ascii=False),
            is_deleted=False,
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        mgmt_deadlines,
        "_compute_engine_deadlines",
        lambda **kwargs: {"FOREIGN_FILING_PARIS": [date(2026, 12, 15)]},
    )
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    foreign_rows = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:STATUS_RED:ForeignFilingDeadline",
    )
    notice_rows = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:FOREIGN_FILING_NOTICE_3M",
    )

    assert len(foreign_rows) == 1
    assert foreign_rows[0].done_date == "2026-02-01"
    assert json.loads(foreign_rows[0].memo or "{}").get("close_reason") == "done"
    assert len(notice_rows) == 1
    assert notice_rows[0].done_date == "2026-02-01"


def test_domestic_application_date_keeps_foreign_filing_deadlines(db_session, monkeypatch):
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        right_group="DOM",
        matter_type="PATENT",
        custom_data={
            "application_date": "2026-02-01",
            "foreign_filing_deadline": "2026-12-15",
            "priority_date": "2026-01-15",
        },
    )
    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    foreign_rows = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:STATUS_RED:ForeignFilingDeadline",
    )
    notice_rows = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:FOREIGN_FILING_NOTICE_3M",
    )

    assert len(foreign_rows) == 1
    assert foreign_rows[0].done_date in (None, "")
    assert len(notice_rows) == 1
    assert notice_rows[0].done_date in (None, "")


def test_registration_status_red_is_saved_as_mixed_category(db_session, monkeypatch):
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        status_red="RegistrationDeadline",
        status_red_related_date="2026-12-15",
    )
    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    rows = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:STATUS_RED:RegistrationDeadline",
    )
    assert len(rows) == 1
    deadline = rows[0]
    assert deadline.due_date == "2026-12-15"
    assert (deadline.category or "").upper() == "MGMT_WORK"


def test_stale_cleanup_keeps_open_registration_deadline_source(db_session, monkeypatch):
    from app.models.docket import DocketItem
    from app.services.deadlines import mgmt_deadlines

    due = date.today() + timedelta(days=90)
    matter_id = _create_patent_matter(
        db_session,
        matter_type="TRADEMARK",
        custom_data={
            "reg_deadline": due.isoformat(),
            "registration_date": "",
            "reg_extension_date": "",
        },
        status_red="RegistrationDeadline",
        status_red_related_date=(date.today() + timedelta(days=30)).isoformat(),
    )
    db_session.add(
        DocketItem(
            docket_id=uuid.uuid4().hex,
            matter_id=str(matter_id),
            category="MGMT_WORK",
            name_ref="MGMT:STATUS_RED:RegistrationDeadline",
            name_free="RegistrationDeadline",
            due_date=due.isoformat(),
            done_date=None,
            memo=json.dumps(
                {
                    "auto": True,
                    "trigger": "status_red",
                    "deadline_code": "REGISTRATION_DEADLINE",
                },
                ensure_ascii=False,
            ),
            is_deleted=False,
        )
    )
    db_session.commit()

    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    rows = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:STATUS_RED:RegistrationDeadline",
    )
    assert len(rows) == 1
    assert (rows[0].done_date or "") == ""


def test_annuity_status_red_is_not_materialized_as_docket(db_session, monkeypatch):
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        status_red="4RenewalDeadline",
        status_red_related_date="2026-03-30",
    )
    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    rows = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:STATUS_RED:4RenewalDeadline",
    )
    assert rows == []


def test_annuity_status_red_legacy_docket_is_auto_closed(db_session, monkeypatch):
    from app.models.docket import DocketItem
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        status_red="4RenewalDeadline",
        status_red_related_date="2026-03-30",
    )

    db_session.add(
        DocketItem(
            docket_id=uuid.uuid4().hex,
            matter_id=str(matter_id),
            category="MGMT",
            name_ref="MGMT:STATUS_RED:4RenewalDeadline",
            name_free="4RenewalDeadline",
            due_date="2026-03-30",
            memo=json.dumps({"auto": True, "trigger": "status_red"}, ensure_ascii=False),
        )
    )
    db_session.commit()

    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    rows = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:STATUS_RED:4RenewalDeadline",
    )
    assert len(rows) == 1
    assert rows[0].done_date == date.today().isoformat()


def test_pct_national_phase_deadline_created_for_pct_matter_only(db_session, monkeypatch):
    from app.services.deadlines import mgmt_deadlines

    pct_matter_id = _create_patent_matter(
        db_session,
        matter_type="PCT",
        custom_data={"national_phase_deadline": "2028-01-01"},
    )
    non_pct_matter_id = _create_patent_matter(
        db_session,
        matter_type="PATENT",
        custom_data={"national_phase_deadline": "2028-01-01"},
    )
    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(pct_matter_id, commit=False)
    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(non_pct_matter_id, commit=False)
    db_session.flush()

    pct_rows = _load_dockets(
        db_session, matter_id=pct_matter_id, name_ref="MGMT:STATUS_RED:PCTDomesticDeadline"
    )
    assert len(pct_rows) == 1
    pct_row = pct_rows[0]
    assert pct_row.due_date == "2028-01-01"
    assert pct_row.visible_from_date == "2027-09-03"
    assert (pct_row.category or "").upper() == "MGMT_WORK"

    non_pct_rows = _load_dockets(
        db_session, matter_id=non_pct_matter_id, name_ref="MGMT:STATUS_RED:PCTDomesticDeadline"
    )
    assert non_pct_rows == []


def test_pct_advisory_19m_system_deadline_is_backfilled_and_kept_open(
    db_session, monkeypatch
):
    from app.models.system_config import SystemConfig
    from app.services.core.config_service import ConfigService
    from app.services.deadlines import mgmt_deadlines

    templates = [
        {
            "id": "NOTICE_SEND_3D",
            "trigger": "office_action_received",
            "offset_days": 3,
            "category": "NOTICE",
            "title": "Notice to client (3 days)",
            "assignee_field": "manager",
        },
        {
            "id": "FOREIGN_FILING_NOTICE_3M",
            "trigger": "deadline_code",
            "deadline_code": "FOREIGN_FILING_PARIS",
            "offset_months": -3,
            "category": "MGMT",
            "title": "Notice to client (3 days)",
            "assignee_field": "manager",
            "skip_if_field_set": "foreign_filing_date",
        },
    ]
    policies = [
        {
            "id": "PCT_ADVISORY_19M",
            "deadline_codes": ["PCT_ADVISORY_19M"],
            "post_due_policy": "AUTO_EXPIRE",
            "effective_due_basis": "due_date",
            "expire_after_days": 0,
            "close_mark": "EXPIRED",
        }
    ]
    SystemConfig.set_config("MGMT_TEMPLATES_JSON", json.dumps(templates, ensure_ascii=False))
    SystemConfig.set_config("DEADLINE_POLICY_JSON", json.dumps(policies, ensure_ascii=False))
    db_session.commit()
    ConfigService.clear_cache()

    overdue = date.today() - timedelta(days=10)
    matter_id = _create_patent_matter(
        db_session,
        matter_type="PCT",
        right_group="ETC",
        custom_data={"national_phase_19m_deadline": overdue.isoformat()},
    )
    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    rows = _load_dockets(
        db_session, matter_id=matter_id, name_ref="MGMT:PCT_ADVISORY_19M"
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.name_free == "Domestic deadline first notice"
    assert row.due_date == overdue.isoformat()
    assert (row.category or "").upper() == "MGMT"
    assert (row.done_date or "") == ""
    memo = json.loads(row.memo or "{}")
    assert memo.get("template_id") == "PCT_ADVISORY_19M"
    assert memo.get("deadline_code") == "PCT_ADVISORY_19M"


def test_pct_national_phase_manual_deadline_beats_stale_status_red_due(
    db_session, monkeypatch
):
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        matter_type="PCT",
        custom_data={"national_phase_deadline": "2028-01-01"},
        status_red="PCTDomesticDeadline",
        status_red_related_date="2028-01-04",
    )
    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    rows = _load_dockets(
        db_session, matter_id=matter_id, name_ref="MGMT:STATUS_RED:PCTDomesticDeadline"
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.due_date == "2028-01-01"
    assert row.visible_from_date == "2027-09-03"
    memo = json.loads(row.memo or "{}")
    assert memo.get("trigger") == "core_deadline"
    assert memo.get("deadline_code") == "PCT_NATIONAL_PHASE"
    assert memo.get("source") == "manual"


def test_stale_cleanup_keeps_open_pct_deadline_source_by_memo_code(db_session, monkeypatch):
    from app.models.docket import DocketItem
    from app.services.deadlines import mgmt_deadlines

    due = date.today() + timedelta(days=420)
    matter_id = _create_patent_matter(
        db_session,
        matter_type="PCT",
        custom_data={"national_phase_last_entry_date": ""},
        status_red="RegistrationDeadline",
        status_red_related_date=(date.today() + timedelta(days=30)).isoformat(),
    )
    db_session.add(
        DocketItem(
            docket_id=uuid.uuid4().hex,
            matter_id=str(matter_id),
            category="MGMT_WORK",
            name_ref="MGMT:STATUS_RED:PCTDomesticDeadline",
            name_free="PCTDomesticDeadline",
            due_date=due.isoformat(),
            done_date=None,
            memo=json.dumps(
                {
                    "auto": True,
                    "trigger": "core_deadline",
                    "deadline_code": "PCT_NATIONAL_PHASE",
                },
                ensure_ascii=False,
            ),
            is_deleted=False,
        )
    )
    db_session.commit()

    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    rows = _load_dockets(
        db_session, matter_id=matter_id, name_ref="MGMT:STATUS_RED:PCTDomesticDeadline"
    )
    assert len(rows) == 1
    assert (rows[0].done_date or "") == ""


def test_pct_national_phase_status_red_due_controls_visible_window(db_session, monkeypatch):
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        matter_type="PCT",
        custom_data={},
        status_red="PCTDomesticDeadline",
        status_red_related_date="2025-05-02",
    )
    monkeypatch.setattr(
        mgmt_deadlines,
        "_compute_engine_deadlines",
        lambda **kwargs: {"PCT_NATIONAL_PHASE": date(2025, 6, 2)},
    )
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    rows = _load_dockets(
        db_session, matter_id=matter_id, name_ref="MGMT:STATUS_RED:PCTDomesticDeadline"
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.due_date == "2025-05-02"
    assert row.visible_from_date == "2025-01-02"


def test_resolve_pct_jurisdiction_out_prefers_primary_country():
    from app.services.deadlines import mgmt_deadlines

    filing_country, designated_country = mgmt_deadlines._resolve_pct_jurisdiction_codes(
        custom_data={
            "application_country": "US",
            "national_phase_countries": "US, US, SG",
        },
        right_group="OUT",
    )

    assert filing_country == "US"
    assert designated_country == "US"


def test_resolve_pct_jurisdiction_out_defaults_to_30m_path_when_missing_country():
    from app.services.deadlines import mgmt_deadlines

    filing_country, designated_country = mgmt_deadlines._resolve_pct_jurisdiction_codes(
        custom_data={},
        right_group="OUT",
    )

    assert filing_country == ""
    assert designated_country is None


def test_resolve_pct_jurisdiction_dom_defaults_to_kr():
    from app.services.deadlines import mgmt_deadlines

    filing_country, designated_country = mgmt_deadlines._resolve_pct_jurisdiction_codes(
        custom_data={},
        right_group="DOM",
    )

    assert filing_country == "US"
    assert designated_country is None


def test_resolve_pct_jurisdiction_pct_etc_storage_defaults_to_kr():
    from app.services.deadlines import mgmt_deadlines

    filing_country, designated_country = mgmt_deadlines._resolve_pct_jurisdiction_codes(
        custom_data={},
        right_group="ETC",
        matter_type="PCT",
    )

    assert filing_country == ""
    assert designated_country is None


def test_compute_engine_deadlines_pct_storage_without_country_uses_30m_path():
    from app.services.deadlines import mgmt_deadlines

    out = mgmt_deadlines._compute_engine_deadlines(
        matter_id=None,
        our_ref="26PD0105PCT",
        custom_data={
            "priority_date": "2023-11-21",
        },
        right_group="ETC",
    )

    assert out["PCT_NATIONAL_PHASE"] == [date(2026, 5, 21)]
    assert "FOREIGN_FILING_PARIS" not in out


def test_compute_engine_deadlines_pct_uses_application_date_as_international_filing_fallback():
    from app.services.deadlines import mgmt_deadlines

    out = mgmt_deadlines._compute_engine_deadlines(
        matter_id=None,
        our_ref="23PD0105PCT",
        custom_data={"application_date": "2023-11-07"},
        right_group="ETC",
    )

    assert out["PCT_NATIONAL_PHASE"] == [date(2026, 5, 7)]
    assert "FOREIGN_FILING_PARIS" not in out


def test_compute_engine_deadlines_pct_matter_type_drives_pct_rule_when_ref_is_ambiguous():
    from app.services.deadlines import mgmt_deadlines

    out = mgmt_deadlines._compute_engine_deadlines(
        matter_id=None,
        our_ref="26PD0105US",
        custom_data={"application_date": "2023-11-07"},
        right_group="ETC",
        matter_type="PCT",
    )

    assert out["PCT_NATIONAL_PHASE"] == [date(2026, 5, 7)]
    assert "FOREIGN_FILING_PARIS" not in out


def test_foreign_notice_3m_applies_default_visible_offset_when_config_missing(
    db_session, monkeypatch
):
    from app.models.system_config import SystemConfig
    from app.services.core.config_service import ConfigService
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        custom_data={"foreign_filing_deadline": "2026-12-15"},
    )

    # Simulate legacy/prod config that lacks visible_offset_days for this template.
    templates = [
        {
            "id": "FOREIGN_FILING_NOTICE_3M",
            "trigger": "deadline_code",
            "deadline_code": "FOREIGN_FILING_PARIS",
            "offset_months": -3,
            "category": "MGMT",
            "title": "Notice to client (3 days)",
            "assignee_field": "manager",
            "skip_if_field_set": "foreign_filing_date",
        }
    ]
    SystemConfig.set_config("MGMT_TEMPLATES_JSON", json.dumps(templates, ensure_ascii=False))
    db_session.commit()
    ConfigService.clear_cache()

    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    rows = _load_dockets(db_session, matter_id=matter_id, name_ref="MGMT:FOREIGN_FILING_NOTICE_3M")
    assert len(rows) == 1
    notice = rows[0]
    assert notice.due_date == "2026-09-15"
    # Default patch: visible 2 weeks before the notice due date
    assert notice.visible_from_date == "2026-09-01"


def test_exam_deadline_visible_from_is_2m_before_due_for_uspto_managed_cases(
    db_session, monkeypatch
):
    from app.services.deadlines import mgmt_deadlines

    dom_matter_id = _create_patent_matter(
        db_session,
        right_group="DOM",
        custom_data={"exam_deadline": "2027-06-05"},
    )
    inc_matter_id = _create_patent_matter(
        db_session,
        right_group="INC",
        custom_data={"exam_deadline": "2027-06-05"},
    )
    pct_matter_id = _create_patent_matter(
        db_session,
        right_group="ETC",
        matter_type="PCT",
        custom_data={"exam_deadline": "2027-06-05"},
    )
    out_matter_id = _create_patent_matter(
        db_session,
        right_group="OUT",
        custom_data={"exam_deadline": "2027-06-05"},
    )

    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(dom_matter_id, commit=False)
    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(inc_matter_id, commit=False)
    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(pct_matter_id, commit=False)
    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(out_matter_id, commit=False)
    db_session.flush()

    dom_rows = _load_dockets(
        db_session,
        matter_id=dom_matter_id,
        name_ref="MGMT:STATUS_RED:Examination requestDeadline",
    )
    assert len(dom_rows) == 1
    assert dom_rows[0].due_date == "2027-06-05"
    assert dom_rows[0].visible_from_date == "2027-04-05"

    inc_rows = _load_dockets(
        db_session,
        matter_id=inc_matter_id,
        name_ref="MGMT:STATUS_RED:Examination requestDeadline",
    )
    assert len(inc_rows) == 1
    assert inc_rows[0].due_date == "2027-06-05"
    assert inc_rows[0].visible_from_date == "2027-04-05"

    pct_rows = _load_dockets(
        db_session,
        matter_id=pct_matter_id,
        name_ref="MGMT:STATUS_RED:Examination requestDeadline",
    )
    assert len(pct_rows) == 1
    assert pct_rows[0].due_date == "2027-06-05"
    assert pct_rows[0].visible_from_date == "2027-04-05"

    out_rows = _load_dockets(
        db_session,
        matter_id=out_matter_id,
        name_ref="MGMT:STATUS_RED:Examination requestDeadline",
    )
    assert len(out_rows) == 1
    assert out_rows[0].due_date == "2027-06-05"
    assert out_rows[0].visible_from_date is None


def test_foreign_filing_expired_followup_template_is_disabled(db_session):
    from app.models.system_config import SystemConfig
    from app.services.core.config_service import ConfigService
    from app.services.deadlines import mgmt_deadlines

    policies = [
        {
            "id": "FOREIGN_FILING_PARIS_MAIN",
            "match": {"name_ref_prefixes": ["MGMT:STATUS_RED:ForeignFilingDeadline"]},
            "post_due_policy": "AUTO_EXPIRE_WITH_FOLLOWUP",
            "effective_due_basis": "due_date",
            "followup_templates": [
                {
                    "id": "FOREIGN_FILING_EXPIRED_NOTICE_3D",
                    "title": "Foreign filing expired notice (3 days)",
                    "offset_days": 3,
                    "category": "SLA",
                    "assignee_field": "manager",
                }
            ],
        }
    ]
    SystemConfig.set_config("DEADLINE_POLICY_JSON", json.dumps(policies, ensure_ascii=False))
    db_session.commit()
    ConfigService.clear_cache()

    loaded = mgmt_deadlines._load_deadline_policies()
    policy = next((p for p in loaded if p.get("id") == "FOREIGN_FILING_PARIS_MAIN"), None)
    assert policy is not None
    assert policy["id"] == "FOREIGN_FILING_PARIS_MAIN"
    assert policy["followup_templates"] == []
    assert policy["post_due_policy"] == "AUTO_EXPIRE"


def test_policy_defaults_patch_adds_exam_and_pct_policies_when_missing(db_session):
    from app.models.system_config import SystemConfig
    from app.services.core.config_service import ConfigService
    from app.services.deadlines import mgmt_deadlines

    policies = [
        {
            "id": "FOREIGN_ONLY",
            "match": {"name_ref_prefixes": ["MGMT:STATUS_RED:ForeignFilingDeadline"]},
            "deadline_codes": ["FOREIGN_FILING_PARIS"],
            "post_due_policy": "AUTO_EXPIRE",
            "effective_due_basis": "due_date",
        }
    ]
    SystemConfig.set_config("DEADLINE_POLICY_JSON", json.dumps(policies, ensure_ascii=False))
    db_session.commit()
    ConfigService.clear_cache()

    loaded = mgmt_deadlines._load_deadline_policies()
    ids = {p.get("id") for p in loaded}
    assert "REQUEST_EXAMINATION_MAIN" in ids
    assert "PCT_NATIONAL_PHASE_MAIN" in ids


def test_auto_close_recovers_legacy_auto_status_red_memo(db_session, monkeypatch):
    from app.models.docket import DocketItem
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        custom_data={"foreign_filing_deadline": "2024-01-01"},
    )

    row = DocketItem(
        matter_id=str(matter_id),
        category="MGMT_WORK",
        name_ref="MGMT:STATUS_RED:ForeignFilingDeadline",
        name_free="ForeignFilingDeadline",
        due_date="2024-01-01",
        done_date=None,
        memo=json.dumps({"auto": True, "trigger": "status_red"}, ensure_ascii=False),
    )
    db_session.add(row)
    db_session.commit()

    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    result = mgmt_deadlines.auto_close_post_due_deadlines(
        matter_id=str(matter_id),
        today=date(2026, 2, 24),
        commit=False,
    )
    db_session.flush()

    closed = DocketItem.query.filter_by(
        matter_id=str(matter_id),
        name_ref="MGMT:STATUS_RED:ForeignFilingDeadline",
    ).first()
    assert closed is not None
    assert closed.done_date == "AUTO_EXPIRED:2026-02-24"
    assert result.get("closed") == 1

    memo = json.loads(closed.memo or "{}")
    assert memo.get("policy_id") == "FOREIGN_FILING_PARIS_MAIN"
    assert memo.get("close_reason") == "expired"


def test_auto_close_recovers_legacy_auto_exam_status_red_memo(db_session, monkeypatch):
    from app.models.docket import DocketItem
    from app.models.system_config import SystemConfig
    from app.services.core.config_service import ConfigService
    from app.services.deadlines import mgmt_deadlines

    policies = [
        {
            "id": "FOREIGN_ONLY",
            "match": {"name_ref_prefixes": ["MGMT:STATUS_RED:ForeignFilingDeadline"]},
            "deadline_codes": ["FOREIGN_FILING_PARIS"],
            "post_due_policy": "AUTO_EXPIRE",
            "effective_due_basis": "due_date",
        }
    ]
    SystemConfig.set_config("DEADLINE_POLICY_JSON", json.dumps(policies, ensure_ascii=False))
    db_session.commit()
    ConfigService.clear_cache()

    matter_id = _create_patent_matter(
        db_session,
        custom_data={"exam_deadline": "2024-01-01"},
    )

    row = DocketItem(
        matter_id=str(matter_id),
        category="MGMT",
        name_ref="MGMT:STATUS_RED:Examination requestDeadline",
        name_free="Examination requestDeadline",
        due_date="2024-01-01",
        done_date=None,
        memo=json.dumps({"auto": True, "trigger": "status_red"}, ensure_ascii=False),
    )
    db_session.add(row)
    db_session.commit()

    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    result = mgmt_deadlines.auto_close_post_due_deadlines(
        matter_id=str(matter_id),
        today=date(2026, 2, 24),
        commit=False,
    )
    db_session.flush()

    closed = DocketItem.query.filter_by(
        matter_id=str(matter_id),
        name_ref="MGMT:STATUS_RED:Examination requestDeadline",
    ).first()
    assert closed is not None
    assert closed.done_date == "AUTO_EXPIRED:2026-02-24"
    assert result.get("closed") == 1

    memo = json.loads(closed.memo or "{}")
    assert memo.get("policy_id") == "REQUEST_EXAMINATION_MAIN"
    assert memo.get("close_reason") == "expired"


def test_status_red_registration_decision_marks_done_when_registration_date_exists(
    db_session, monkeypatch
):
    from app.models.docket import DocketItem
    from app.models.ip_records import Matter, MatterCustomField
    from app.services.deadlines import mgmt_deadlines

    matter_id = uuid.uuid4().hex
    matter = Matter(
        matter_id=matter_id,
        our_ref=f"26TD{uuid.uuid4().hex[:4].upper()}US",
        right_name="Text Text Text",
        right_group="DOM",
        matter_type="TRADEMARK",
        retained_at="2025-01-01",
        entered_at="2025-01-01",
        status_red="RegistrationDeadline",
        status_red_related_date="2025-11-01",
        status_blue="",
        is_deleted=False,
    )
    db_session.add(matter)
    db_session.add(
        MatterCustomField(
            matter_id=matter_id,
            namespace="domestic_trademark",
            data={"registration_date": "2025-09-05"},
        )
    )
    db_session.commit()

    row = DocketItem(
        matter_id=str(matter_id),
        category="MGMT_WORK",
        name_ref="MGMT:STATUS_RED:RegistrationDeadline",
        name_free="RegistrationDeadline",
        due_date="2025-11-01",
        done_date=None,
        memo=json.dumps({"auto": True, "trigger": "status_red"}, ensure_ascii=False),
    )
    db_session.add(row)
    db_session.flush()

    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(
        mgmt_deadlines,
        "_merge_custom_fields",
        lambda _matter_id: {"registration_date": "2025-09-05"},
    )
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    closed = DocketItem.query.filter_by(
        matter_id=str(matter_id),
        name_ref="MGMT:STATUS_RED:RegistrationDeadline",
    ).first()
    assert closed is not None
    assert closed.done_date == "2025-09-05"


def test_status_red_registration_success_marks_done_when_registration_date_exists(
    db_session, monkeypatch
):
    from app.models.docket import DocketItem
    from app.models.ip_records import Matter, MatterCustomField
    from app.services.deadlines import mgmt_deadlines

    matter_id = uuid.uuid4().hex
    matter = Matter(
        matter_id=matter_id,
        our_ref=f"26TD{uuid.uuid4().hex[:4].upper()}US",
        right_name="Text Text Text",
        right_group="DOM",
        matter_type="TRADEMARK",
        retained_at="2025-01-01",
        entered_at="2025-01-01",
        status_red="RegistrationDeadline",
        status_red_related_date="2025-11-01",
        status_blue="Text",
        is_deleted=False,
    )
    db_session.add(matter)
    db_session.add(
        MatterCustomField(
            matter_id=matter_id,
            namespace="domestic_trademark",
            data={"registration_date": "2025-09-05"},
        )
    )
    db_session.commit()

    row = DocketItem(
        matter_id=str(matter_id),
        category="MGMT_WORK",
        name_ref="MGMT:STATUS_RED:RegistrationDeadline",
        name_free="RegistrationDeadline",
        due_date="2025-11-01",
        done_date=None,
        memo=json.dumps({"auto": True, "trigger": "status_red"}, ensure_ascii=False),
    )
    db_session.add(row)
    db_session.flush()

    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(
        mgmt_deadlines,
        "_merge_custom_fields",
        lambda _matter_id: {"registration_date": "2025-09-05"},
    )
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    closed = DocketItem.query.filter_by(
        matter_id=str(matter_id),
        name_ref="MGMT:STATUS_RED:RegistrationDeadline",
    ).first()
    assert closed is not None
    assert closed.done_date == "2025-09-05"


def test_priority_exam_progress_status_red_tracks_application_plus_7_days(db_session, monkeypatch):
    from app.models.ip_records import MatterCustomField
    from app.services.deadlines import mgmt_deadlines

    matter_id = _create_patent_matter(
        db_session,
        custom_data={
            "priority_exam_request": "Yes",
            "application_date": "2026-04-11",
        },
    )
    monkeypatch.setattr(mgmt_deadlines, "_compute_engine_deadlines", lambda **kwargs: {})
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    rows = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:STATUS_RED:ExaminationOpen",
    )
    assert len(rows) == 1
    assert rows[0].due_date == "2026-04-18"
    assert not (rows[0].done_date or "").strip()

    row = MatterCustomField.query.filter_by(
        matter_id=matter_id, namespace="domestic_patent"
    ).first()
    assert row is not None
    data = dict(row.data or {})
    data["expedited_request_date"] = "2026-04-15"
    row.data = data
    db_session.add(row)
    db_session.commit()

    mgmt_deadlines.ensure_mgmt_deadlines_for_matter(matter_id, commit=False)
    db_session.flush()

    rows_after_done = _load_dockets(
        db_session,
        matter_id=matter_id,
        name_ref="MGMT:STATUS_RED:ExaminationOpen",
    )
    assert len(rows_after_done) == 1
    assert rows_after_done[0].done_date == "2026-04-15"
