from app.models.docket import DocketItem
from app.models.matter import Matter, MatterCustomField
from app.models.system_config import SystemConfig
from app.services.matter import matter_status_cache as matter_status_cache_service
from app.services.matter.matter_status_cache import (
    apply_auto_status_cache_to_matter,
    audit_matter_status_cache_window,
    reconcile_matter_status_cache_batch,
)


def _make_outgoing_patent(
    db_session,
    *,
    our_ref: str,
    status_blue: str,
    status_red: str = "FilingDeadline",
    status_red_related_date: str = "2026-05-01",
    payload: dict | None = None,
) -> Matter:
    matter = Matter(
        matter_id=our_ref.lower(),
        our_ref=our_ref,
        right_name="Text Text",
        right_group="OUT",
        matter_type="PATENT",
        status_red=status_red,
        status_red_related_date=status_red_related_date,
        status_blue=status_blue,
    )
    db_session.add(matter)
    db_session.add(
        MatterCustomField(
            matter_id=matter.matter_id,
            namespace="outgoing_patent",
            data=payload
            or {
                "exam_requested": "Y",
                "filing_deadline": status_red_related_date,
                "application_date": "",
                "exam_request_date": "",
            },
        )
    )
    db_session.commit()
    db_session.refresh(matter)
    return matter


def test_apply_auto_status_cache_to_matter_fixes_stale_under_exam_blue(app, db_session):
    matter = _make_outgoing_patent(
        db_session,
        our_ref="26POFIX01US",
        status_blue="Filing Examination In Progress",
    )

    result = apply_auto_status_cache_to_matter(matter=matter)

    assert result.changed is True
    assert "status_blue" in result.fields_changed
    assert result.auto_status.status_blue == "Filing  In Progress"
    assert matter.status_blue == "Filing  In Progress"


def test_apply_auto_status_cache_to_matter_clears_orphan_filing_red(app, db_session):
    matter = _make_outgoing_patent(
        db_session,
        our_ref="26POFIX01AUS",
        status_blue="Filing  In Progress",
    )

    result = apply_auto_status_cache_to_matter(matter=matter)

    assert result.changed is True
    assert result.auto_status.status_red == ""
    assert (matter.status_red or "") == ""


def test_apply_auto_status_cache_to_matter_keeps_filing_red_with_open_filing_docket(
    app, db_session
):
    matter = _make_outgoing_patent(
        db_session,
        our_ref="26POFIX01BUS",
        status_blue="Filing  In Progress",
    )
    db_session.add(
        DocketItem(
            docket_id="filing_docket_1",
            matter_id=matter.matter_id,
            category="FILING",
            name_ref="Filing",
            name_free="FilingDeadline",
            due_date="2026-05-01",
        )
    )
    db_session.commit()

    result = apply_auto_status_cache_to_matter(matter=matter)

    assert result.auto_status.status_red == "FilingDeadline"
    assert matter.status_red == "FilingDeadline"
    assert matter.status_red_related_date == "2026-05-01"


def test_apply_auto_status_cache_to_matter_hides_foreign_filing_red_before_visible_window(
    app, db_session, monkeypatch
):
    from datetime import date

    monkeypatch.setattr(
        "app.services.matter.matter_auto_status._today",
        lambda: date(2026, 4, 23),
    )

    matter = _make_outgoing_patent(
        db_session,
        our_ref="26POFIX01CUS",
        status_blue="ForeignFiling  In Progress",
        status_red="ForeignFilingDeadline",
        status_red_related_date="2026-12-15",
        payload={
            "filing_deadline": "2026-01-10",
            "application_date": "2026-01-10",
            "foreign_filing_deadline": "2026-12-15",
            "exam_requested": "",
            "exam_request_date": "",
        },
    )

    result = apply_auto_status_cache_to_matter(matter=matter)

    assert result.changed is True
    assert result.auto_status.status_red == ""
    assert result.auto_status.status_blue == "Filing Examination In Progress"
    assert (matter.status_red or "") == ""
    assert matter.status_blue == "Filing Examination In Progress"


def test_apply_auto_status_cache_to_matter_clears_internal_mgmt_notice_ref(
    app, db_session, monkeypatch
):
    from datetime import date

    monkeypatch.setattr(
        "app.services.matter.matter_auto_status._today",
        lambda: date(2026, 4, 23),
    )

    matter = _make_outgoing_patent(
        db_session,
        our_ref="26POFIX01DUS",
        status_blue="Filing Examination In Progress",
        status_red="MGMT:FOREIGN_FILING_NOTICE_3M",
        status_red_related_date="2027-01-22",
        payload={
            "application_date": "2026-04-22",
            "exam_requested": "Y",
            "exam_request_date": "2026-04-22",
            "foreign_filing_deadline": "2027-04-22",
        },
    )

    result = apply_auto_status_cache_to_matter(matter=matter)

    assert result.changed is True
    assert result.auto_status.status_red == ""
    assert result.auto_status.status_red_related_date == ""
    assert (matter.status_red or "") == ""
    assert (matter.status_red_related_date or "") == ""


def test_reconcile_matter_status_cache_batch_updates_only_drifted_matters(app, db_session):
    stale = _make_outgoing_patent(
        db_session,
        our_ref="26POFIX02US",
        status_blue="Filing Examination In Progress",
    )
    correct = _make_outgoing_patent(
        db_session,
        our_ref="26POFIX03US",
        status_blue="Filing  In Progress",
    )

    result = reconcile_matter_status_cache_batch(commit=True, commit_interval=1)

    db_session.refresh(stale)
    db_session.refresh(correct)

    assert result["processed"] >= 2
    assert result["updated"] >= 1
    assert result["errors"] == 0
    assert result["changed_fields"]["status_blue"] >= 1
    assert stale.status_blue == "Filing  In Progress"
    assert correct.status_blue == "Filing  In Progress"


def test_reconcile_matter_status_cache_batch_streams_ids_in_pages(app, db_session, monkeypatch):
    _make_outgoing_patent(
        db_session,
        our_ref="26POFIX04US",
        status_blue="Filing  In Progress",
    )
    _make_outgoing_patent(
        db_session,
        our_ref="26POFIX05US",
        status_blue="Filing  In Progress",
    )
    _make_outgoing_patent(
        db_session,
        our_ref="26POFIX06US",
        status_blue="Filing  In Progress",
    )

    calls: list[tuple[int, str | None]] = []
    original = matter_status_cache_service._fetch_matter_status_cache_window_ids

    def _patched_fetch(*, limit: int, start_after_matter_id: str | None = None):
        calls.append((limit, start_after_matter_id))
        return original(limit=limit, start_after_matter_id=start_after_matter_id)

    monkeypatch.setattr(
        matter_status_cache_service,
        "_fetch_matter_status_cache_window_ids",
        _patched_fetch,
    )

    result = reconcile_matter_status_cache_batch(
        limit=3,
        commit=True,
        commit_interval=1,
        page_size=1,
    )

    assert result["processed"] == 3
    assert len(calls) >= 3
    assert calls[0] == (1, None)
    assert calls[1][1] is not None


def test_audit_matter_status_cache_window_advances_cursor_and_wraps(app, db_session):
    first = _make_outgoing_patent(
        db_session,
        our_ref="26POFIX11US",
        status_blue="Filing  In Progress",
    )
    second = _make_outgoing_patent(
        db_session,
        our_ref="26POFIX12US",
        status_blue="Filing  In Progress",
    )

    cursor_key = "TEST_MATTER_STATUS_CACHE_AUDIT_CURSOR"

    first_result = audit_matter_status_cache_window(
        limit=1,
        commit=True,
        commit_interval=1,
        cursor_key=cursor_key,
    )
    db_session.refresh(first)
    db_session.refresh(second)

    assert first_result["processed"] == 1
    assert first_result["cursor_before"] == ""
    assert first_result["cursor_after"] == first.matter_id
    assert first.status_blue == "Filing  In Progress"
    assert second.status_blue == "Filing  In Progress"
    assert SystemConfig.get_config(cursor_key) == first.matter_id

    second_result = audit_matter_status_cache_window(
        limit=1,
        commit=True,
        commit_interval=1,
        cursor_key=cursor_key,
    )
    db_session.refresh(second)

    assert second_result["processed"] == 1
    assert second_result["cursor_before"] == first.matter_id
    assert second_result["cursor_after"] == second.matter_id
    assert second.status_blue == "Filing  In Progress"

    wrapped_result = audit_matter_status_cache_window(
        limit=1,
        commit=True,
        commit_interval=1,
        cursor_key=cursor_key,
    )

    assert wrapped_result["processed"] == 1
    assert wrapped_result["wrapped"] is True


def test_audit_matter_status_cache_window_stops_cursor_before_failed_row(
    app, db_session, monkeypatch
):
    first = _make_outgoing_patent(
        db_session,
        our_ref="26POFIX21US",
        status_blue="Filing  In Progress",
    )
    second = _make_outgoing_patent(
        db_session,
        our_ref="26POFIX22US",
        status_blue="Filing  In Progress",
    )

    original = matter_status_cache_service.apply_auto_status_cache_to_matter

    def _patched_apply(*, matter, **kwargs):
        if str(matter.matter_id) == second.matter_id:
            raise RuntimeError("simulated audit failure")
        return original(matter=matter, **kwargs)

    monkeypatch.setattr(
        matter_status_cache_service,
        "apply_auto_status_cache_to_matter",
        _patched_apply,
    )

    cursor_key = "TEST_MATTER_STATUS_CACHE_AUDIT_CURSOR_FAILURE"
    result = audit_matter_status_cache_window(
        limit=2,
        commit=True,
        commit_interval=100,
        cursor_key=cursor_key,
    )

    db_session.refresh(first)
    db_session.refresh(second)

    assert result["processed"] == 2
    assert result["errors"] == 1
    assert result["cursor_after"] == first.matter_id
    assert SystemConfig.get_config(cursor_key) == first.matter_id
    assert first.status_blue == "Filing  In Progress"
    assert second.status_blue == "Filing  In Progress"
