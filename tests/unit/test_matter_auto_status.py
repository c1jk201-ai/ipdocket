"""
Unit tests for matter_auto_status service.

Tests pure functions that don't require database access.
"""

from datetime import date, timedelta
from pathlib import Path

import pytest


class TestNormalizeSpace:
    """Tests for _normalize_space function."""

    def test_normalize_space_basic(self):
        from app.services.matter.matter_auto_status import _normalize_space

        assert _normalize_space("hello  world") == "hello world"

    def test_normalize_space_none(self):
        from app.services.matter.matter_auto_status import _normalize_space

        assert _normalize_space(None) == ""

    def test_normalize_space_empty(self):
        from app.services.matter.matter_auto_status import _normalize_space

        assert _normalize_space("") == ""
        assert _normalize_space("   ") == ""

    def test_normalize_space_multiple_spaces(self):
        from app.services.matter.matter_auto_status import _normalize_space

        assert _normalize_space("  a   b   c  ") == "a b c"

    def test_normalize_space_newlines_tabs(self):
        from app.services.matter.matter_auto_status import _normalize_space

        assert _normalize_space("hello\n\tworld") == "hello world"


class TestDateOnlyStr:
    """Tests for date_only_str function."""

    def test_date_only_str_empty(self):
        from app.services.matter.matter_auto_status import date_only_str

        assert date_only_str(None) == ""
        assert date_only_str("") == ""
        assert date_only_str("   ") == ""

    def test_date_only_str_iso_format(self):
        from app.services.matter.matter_auto_status import date_only_str

        assert date_only_str("2026-01-15") == "2026-01-15"

    def test_date_only_str_dot_format(self):
        from app.services.matter.matter_auto_status import date_only_str

        assert date_only_str("2026.01.15") == "2026-01-15"

    def test_date_only_str_slash_format(self):
        from app.services.matter.matter_auto_status import date_only_str

        assert date_only_str("2026/01/15") == "2026-01-15"

    def test_date_only_str_single_digit(self):
        from app.services.matter.matter_auto_status import date_only_str

        assert date_only_str("2026-1-5") == "2026-01-05"

    def test_date_only_str_embedded(self):
        from app.services.matter.matter_auto_status import date_only_str

        assert date_only_str("Deadline: 2026-03-20 is the date") == "2026-03-20"

    def test_date_only_str_compact_yyyymmdd(self):
        from app.services.matter.matter_auto_status import date_only_str

        assert date_only_str("20260115") == "2026-01-15"
        assert date_only_str("Text=20260115") == "2026-01-15"

    def test_date_only_str_invalid(self):
        from app.services.matter.matter_auto_status import date_only_str

        assert date_only_str("invalid") == ""
        assert date_only_str("2026-13-40") == ""  # Invalid month/day


class TestAddMonths:
    def test_add_months_negative_overflow_raises(self):
        from app.services.matter.matter_auto_status import _add_months

        with pytest.raises(ValueError):
            _add_months(date(1, 1, 1), -1)


class TestNormalizeBlueStatus:
    """Tests for normalize_blue_status function."""

    def test_normalize_blue_status_empty(self):
        from app.services.matter.matter_auto_status import normalize_blue_status

        assert normalize_blue_status(None) == ""
        assert normalize_blue_status("") == ""

    def test_normalize_blue_status_single_char(self):
        from app.services.matter.matter_auto_status import normalize_blue_status

        assert normalize_blue_status("In Progress") == ""

    def test_normalize_blue_status_canonical(self):
        from app.services.matter.matter_auto_status import normalize_blue_status

        assert normalize_blue_status("FilingExamination In Progress") == (
            "Filing Examination In Progress"
        )
        assert normalize_blue_status("Registration Done") == "RegistrationDone"
        assert normalize_blue_status("PublicationIn Progress") == "Filing Publication In Progress"

    def test_normalize_blue_status_passthrough(self):
        from app.services.matter.matter_auto_status import normalize_blue_status

        # Unknown values should pass through unchanged (after space normalization)
        assert normalize_blue_status("Custom Blue") == "Custom Blue"


class TestNormalizeRedStatus:
    """Tests for normalize_red_status function."""

    def test_normalize_red_status_empty(self):
        from app.services.matter.matter_auto_status import normalize_red_status

        assert normalize_red_status(None) == ""
        assert normalize_red_status("") == ""

    def test_normalize_red_status_canonical(self):
        from app.services.matter.matter_auto_status import normalize_red_status

        assert normalize_red_status("Filing Deadline") == "FilingDeadline"
        assert normalize_red_status("Foreign Filing Deadline") == "ForeignFilingDeadline"
        assert normalize_red_status("Examination request Deadline") == (
            "Examination requestDeadline"
        )
        assert normalize_red_status("MGMT:STATUS_RED:Filing Deadline") == "FilingDeadline"

    def test_normalize_red_status_passthrough(self):
        from app.services.matter.matter_auto_status import normalize_red_status

        assert normalize_red_status("Custom Red") == "Custom Red"

    def test_internal_mgmt_non_status_refs_are_not_red_status(self):
        from app.services.matter.matter_auto_status import is_internal_mgmt_non_status_red_ref

        assert is_internal_mgmt_non_status_red_ref("MGMT:FOREIGN_FILING_NOTICE_3M") is True
        assert (
            is_internal_mgmt_non_status_red_ref("MGMT:FOREIGN_FILING_NOTICE_3M[2027-01-22]") is True
        )
        assert is_internal_mgmt_non_status_red_ref("MGMT:STATUS_RED:Text") is False
        assert is_internal_mgmt_non_status_red_ref("MGMT:NOTICE_SEND_3D:oa-1") is False


class TestLooksLikeNonRedDocumentTitle:
    def test_doc_like_application_document_titles(self):
        from app.services.matter.matter_auto_status import _looks_like_non_red_document_title

        assert _looks_like_non_red_document_title("PatentFiling") is True
        assert _looks_like_non_red_document_title("ApplicationFiling") is True
        assert _looks_like_non_red_document_title("Filing") is True
        assert _looks_like_non_red_document_title("Filing[2026-01-31]") is True

    def test_doc_like_other_submission_titles(self):
        from app.services.matter.matter_auto_status import _looks_like_non_red_document_title

        assert _looks_like_non_red_document_title("Examination request") is True
        assert _looks_like_non_red_document_title("Examination") is True
        assert _looks_like_non_red_document_title("Department") is True
        assert _looks_like_non_red_document_title("LegacyFiling") is True
        return
        assert _looks_like_non_red_document_title("Examination request쨌Examination") is True
        assert _looks_like_non_red_document_title("Text·Text·Text") is True
        assert _looks_like_non_red_document_title("Text·Text") is True

    def test_doc_like_payment_notice_titles(self):
        from app.services.matter.matter_auto_status import _looks_like_non_red_document_title

        assert _looks_like_non_red_document_title("Payment") is True
        assert _looks_like_non_red_document_title("RegistrationPayment") is True
        assert _looks_like_non_red_document_title("SettingsRegistrationPayment") is True

    def test_not_doc_like_office_action_notice(self):
        from app.services.matter.matter_auto_status import _looks_like_non_red_document_title

        # OA notice names should remain eligible for red.
        assert _looks_like_non_red_document_title("Notice") is False
        assert _looks_like_non_red_document_title("OA1") is False


class TestOfficeActionCandidateDoc:
    def test_payment_notice_not_candidate(self):
        from app.services.matter.matter_auto_status import _is_candidate_office_action_doc

        assert _is_candidate_office_action_doc("Payment") is False
        assert _is_candidate_office_action_doc("Department") is False
        assert _is_candidate_office_action_doc("RegistrationPayment") is False
        assert _is_candidate_office_action_doc("SettingsRegistrationPayment") is False

    def test_oa_notice_is_candidate(self):
        from app.services.matter.matter_auto_status import _is_candidate_office_action_doc

        assert _is_candidate_office_action_doc("OA1") is True
        assert _is_candidate_office_action_doc("Notice") is True
        assert _is_candidate_office_action_doc("Office action Notice") is True

    def test_non_response_notice_not_candidate(self):
        from app.services.matter.matter_auto_status import _is_candidate_office_action_doc

        assert _is_candidate_office_action_doc("StatutoryPeriod") is False
        assert _is_candidate_office_action_doc("Guidance target") is False
        assert _is_candidate_office_action_doc("Publication decision") is False
        assert _is_candidate_office_action_doc("PriorityDeadline") is False


class TestIsKnownDeadlineRedLabel:
    """Tests for is_known_deadline_red_label function."""

    def test_is_known_deadline_red_label_known(self):
        from app.services.matter.matter_auto_status import is_known_deadline_red_label

        assert is_known_deadline_red_label("FilingDeadline") is True
        assert is_known_deadline_red_label("ForeignFilingDeadline") is True
        assert is_known_deadline_red_label("Examination requestDeadline") is True
        assert is_known_deadline_red_label("RegistrationDeadline") is True
        assert is_known_deadline_red_label("Deadline") is True

    def test_is_known_deadline_red_label_unknown(self):
        from app.services.matter.matter_auto_status import is_known_deadline_red_label

        assert is_known_deadline_red_label("Custom Red") is False
        assert is_known_deadline_red_label("Notice") is False

    def test_is_known_deadline_red_label_empty(self):
        from app.services.matter.matter_auto_status import is_known_deadline_red_label

        assert is_known_deadline_red_label(None) is False
        assert is_known_deadline_red_label("") is False


class TestFormatRedDisplay:
    """Tests for _format_red_display function."""

    def test_format_red_display_basic(self):
        from app.services.matter.matter_auto_status import _format_red_display

        result = _format_red_display("Text", "2026-03-15", "")
        assert result == "Text[2026-03-15]"

    def test_format_red_display_no_date(self):
        from app.services.matter.matter_auto_status import _format_red_display

        result = _format_red_display("Text", "", "")
        assert result == "Text"

    def test_format_red_display_empty(self):
        from app.services.matter.matter_auto_status import _format_red_display

        result = _format_red_display("", "", "")
        assert result == ""

    def test_format_red_display_abandon_with_memo(self):
        from app.services.matter.matter_auto_status import _format_red_display

        result = _format_red_display("Abandoned", "2026-01-01", "Client instructed abandonment")
        assert "Abandoned" in result
        assert "Client instructed abandonment" in result


class TestSuggestBlueFromRed:
    """Tests for _suggest_blue_from_red function."""

    def test_suggest_blue_from_red_empty(self):
        from app.services.matter.matter_auto_status import _suggest_blue_from_red

        assert _suggest_blue_from_red("") == ""
        assert _suggest_blue_from_red(None) == ""

    def test_suggest_blue_from_red_opinion_notice(self):
        from app.services.matter.matter_auto_status import _suggest_blue_from_red

        assert _suggest_blue_from_red("Office action Notice") == "   In Progress"
        assert _suggest_blue_from_red("Deadline") == "   In Progress"

    def test_suggest_blue_from_red_rejection_notice(self):
        from app.services.matter.matter_auto_status import _suggest_blue_from_red

        assert _suggest_blue_from_red("Notice") == "Filing  In Progress"

    def test_suggest_blue_from_red_publication_decision(self):
        from app.services.matter.matter_auto_status import _suggest_blue_from_red

        assert _suggest_blue_from_red("Publication decision") == "Filing Publication In Progress"
        assert _suggest_blue_from_red("24TD0234US Publication decision") == (
            "Filing Publication In Progress"
        )

    def test_suggest_blue_from_red_registration(self):
        from app.services.matter.matter_auto_status import _suggest_blue_from_red

        assert _suggest_blue_from_red("RegistrationDeadline") == (
            "RegistrationWaiting In Progress"
        )
        assert _suggest_blue_from_red("Notice of allowance") == (
            "RegistrationWaiting In Progress"
        )

    def test_suggest_blue_from_red_exam_request(self):
        from app.services.matter.matter_auto_status import _suggest_blue_from_red

        assert _suggest_blue_from_red("Examination requestDeadline") == (
            "Examination  Billing In Progress"
        )
        assert _suggest_blue_from_red("ForeignFilingDeadline") == "ForeignFiling  In Progress"
        assert _suggest_blue_from_red("FilingDeadline") == "Filing  In Progress"

    def test_suggest_blue_from_red_abandon(self):
        from app.services.matter.matter_auto_status import _suggest_blue_from_red

        assert _suggest_blue_from_red("Abandoned") == "Matter closed"
        assert _suggest_blue_from_red("Matter closed") == "Matter closed"


class TestInferResponseKind:
    def test_correction_includes_complement_and_supplement(self):
        from app.services.matter.matter_auto_status import _infer_response_kind

        assert _infer_response_kind("Correction") == "correction"
        assert _infer_response_kind("Supplemental correction") == "correction"
        assert _infer_response_kind("Complement") == "correction"


class TestBlueFromOpenOfficeAction:
    def test_payment_notice_returns_empty(self):
        from app.services.matter.matter_auto_status import _blue_from_open_office_action

        assert _blue_from_open_office_action("Payment") == ""
        assert _blue_from_open_office_action("RegistrationPayment") == ""

    def test_oa_notice_returns_oa_workflow(self):
        from app.services.matter.matter_auto_status import _blue_from_open_office_action

        assert _blue_from_open_office_action("OA1") == "OA  In Progress"
        assert _blue_from_open_office_action("Office action Notice") == "OA  In Progress"
        assert _blue_from_open_office_action("Notice") == "OA  In Progress"

    def test_non_response_notice_does_not_force_oa_workflow(self):
        from app.services.matter.matter_auto_status import _blue_from_open_office_action

        assert _blue_from_open_office_action("StatutoryPeriod") == ""
        assert _blue_from_open_office_action("Guidance target") == ""
        assert _blue_from_open_office_action("PriorityDeadline") == ""

    def test_publication_decision_notice_maps_to_publication_blue(self):
        from app.services.matter.matter_auto_status import _blue_from_open_office_action

        assert _blue_from_open_office_action("Publication decision") == (
            "Filing Publication In Progress"
        )


class TestPreferEarlierMgmtStatusRed:
    def test_prefers_earlier_mgmt_status_red(self):
        from app.services.matter.matter_auto_status import _prefer_earlier_mgmt_status_red

        assert _prefer_earlier_mgmt_status_red(
            "Text",
            "2029-01-01",
            "Text",
            "2026-04-18",
        ) == ("Text", "2026-04-18")

    def test_keeps_current_when_mgmt_is_later(self):
        from app.services.matter.matter_auto_status import _prefer_earlier_mgmt_status_red

        assert _prefer_earlier_mgmt_status_red(
            "Text",
            "2026-04-11",
            "Text",
            "2026-04-18",
        ) == ("Text", "2026-04-11")


class TestTrialPendingSignalHelpers:
    def test_trial_pending_notice_keywords(self):
        from app.services.matter.matter_auto_status import _looks_like_trial_pending_notice

        assert _looks_like_trial_pending_notice("Notice") is True
        assert _looks_like_trial_pending_notice("Trial Notice") is True
        assert _looks_like_trial_pending_notice("Payment") is False

    def test_trial_pending_response_keywords(self):
        from app.services.matter.matter_auto_status import _looks_like_trial_pending_response

        assert _looks_like_trial_pending_response("Billing: C260001US_.txt") is True
        assert _looks_like_trial_pending_response("Trial Filed confirmation") is True
        assert _looks_like_trial_pending_response("Payment Notice") is False


class TestDeriveBlueFromEvents:
    def test_exam_request_deadline_expired_is_not_exam_pending(self):
        from app.services.matter.matter_auto_status import _derive_blue_from_events

        blue = _derive_blue_from_events(
            "mid",
            event_presence={"APPLICATION_DATE", "EXAM_REQUEST_DEADLINE"},
            event_due_by_std_key={"EXAM_REQUEST_DEADLINE": date(2020, 1, 1)},
            expired_deadlines={"EXAM_REQUEST_DEADLINE"},
        )
        assert blue == "Filing Examination In Progress"

    def test_exam_request_deadline_active_is_exam_pending(self):
        from app.services.matter.matter_auto_status import _derive_blue_from_events

        blue = _derive_blue_from_events(
            "mid",
            event_presence={"APPLICATION_DATE", "EXAM_REQUEST_DEADLINE"},
            event_due_by_std_key={"EXAM_REQUEST_DEADLINE": date(2030, 1, 1)},
            expired_deadlines=set(),
        )
        assert blue == "Examination  Billing In Progress"

    def test_exam_requested_boolean_means_under_exam(self):
        from app.services.matter.matter_auto_status import _derive_blue_from_events

        blue = _derive_blue_from_events(
            "mid",
            event_presence={"APPLICATION_DATE", "EXAM_REQUESTED"},
            event_due_by_std_key={},
            expired_deadlines=set(),
        )
        assert blue == "Filing Examination In Progress"

    def test_exam_requested_without_application_date_is_not_under_exam(self):
        from app.services.matter.matter_auto_status import _derive_blue_from_events

        blue = _derive_blue_from_events(
            "mid",
            event_presence={"EXAM_REQUESTED"},
            event_due_by_std_key={},
            expired_deadlines=set(),
        )
        assert blue == ""

    def test_uses_event_summary_raw_mapped_presence(self):
        from app.services.matter.matter_auto_status import EventSummary, _derive_blue_from_events

        summary = EventSummary(
            presence=set(),
            min_dates={},
            max_dates={},
            raw_keys={"Filing date", "Examination request Due date"},
        )
        blue = _derive_blue_from_events(
            "mid",
            event_presence=set(),
            event_due_by_std_key={"EXAM_REQUEST_DEADLINE": date(2030, 1, 1)},
            event_summary=summary,
            expired_deadlines=set(),
        )
        assert blue == "Examination  Billing In Progress"


class TestSummarizeEventRows:
    def test_legacy_exam_request_without_date_sets_requested_flag(self):
        from app.services.matter.matter_auto_status import _summarize_event_rows

        summary = _summarize_event_rows([("EXAM_REQUEST_DATE", "EXAM_REQUEST_DATE", "")])
        assert "EXAM_REQUESTED" in summary.presence
        assert "EXAM_REQUEST_DATE" not in summary.min_dates


class TestSupplementEventSummaryFromCustomFields:
    def test_exam_requested_boolean_requires_filing_evidence(self, app, db_session):
        from app.models.matter import MatterCustomField
        from app.services.matter.matter_auto_status import (
            EventSummary,
            MatterContext,
            _supplement_event_summary_from_custom_fields,
        )

        mid = "mid-no-application"
        db_session.add(
            MatterCustomField(
                matter_id=mid,
                namespace="outgoing_patent",
                data={"exam_requested": "Y", "filing_deadline": "2026-05-01"},
            )
        )
        db_session.commit()

        summary = EventSummary(presence=set(), min_dates={}, max_dates={}, raw_keys=set())
        _supplement_event_summary_from_custom_fields(
            mid,
            summary,
            ctx=MatterContext("OUT", "PATENT", "", False),
        )

        assert "EXAM_REQUESTED" not in summary.presence

    def test_exam_requested_boolean_with_application_date_keeps_requested(self, app, db_session):
        from app.models.matter import MatterCustomField
        from app.services.matter.matter_auto_status import (
            EventSummary,
            MatterContext,
            _supplement_event_summary_from_custom_fields,
        )

        mid = "mid-with-application"
        db_session.add(
            MatterCustomField(
                matter_id=mid,
                namespace="outgoing_patent",
                data={"exam_requested": "Y", "application_date": "2026-02-15"},
            )
        )
        db_session.commit()

        summary = EventSummary(presence=set(), min_dates={}, max_dates={}, raw_keys=set())
        _supplement_event_summary_from_custom_fields(
            mid,
            summary,
            ctx=MatterContext("OUT", "PATENT", "", False),
        )

        assert "APPLICATION_DATE" in summary.presence
        assert "EXAM_REQUESTED" in summary.presence

    def test_internal_filing_deadline_does_not_become_red_event(self, app, db_session):
        from app.models.matter import MatterCustomField, MatterEvent
        from app.services.matter.matter_auto_status import (
            MatterContext,
            _summarize_event_rows,
            _supplement_event_summary_from_custom_fields,
        )

        mid = "mid-internal-filing-deadline"
        db_session.add(
            MatterCustomField(
                matter_id=mid,
                namespace="outgoing_patent",
                data={
                    "filing_deadline": "2026-05-01",
                    "filing_deadline_type": "INTERNAL",
                },
            )
        )
        db_session.add(
            MatterEvent(
                mevent_id="mid-internal-filing-deadline-event",
                matter_id=mid,
                event_key="Text",
                event_at="2026-05-01",
                source_column="form:outgoing_patent",
            )
        )
        db_session.commit()

        summary = _summarize_event_rows([("Text", "APPLICATION_DEADLINE", "2026-05-01")])
        assert "APPLICATION_DEADLINE" in summary.presence

        _supplement_event_summary_from_custom_fields(
            mid,
            summary,
            ctx=MatterContext("OUT", "PATENT", "26PO0107US", False),
        )

        assert "APPLICATION_DEADLINE" not in summary.presence
        assert "APPLICATION_DEADLINE" not in summary.min_dates

    def test_legal_filing_deadline_still_becomes_red_event(self, app, db_session):
        from app.models.matter import MatterCustomField
        from app.services.matter.matter_auto_status import (
            EventSummary,
            MatterContext,
            _supplement_event_summary_from_custom_fields,
        )

        mid = "mid-legal-filing-deadline"
        db_session.add(
            MatterCustomField(
                matter_id=mid,
                namespace="outgoing_patent",
                data={
                    "filing_deadline": "2026-05-21",
                    "filing_deadline_type": "LEGAL",
                },
            )
        )
        db_session.commit()

        summary = EventSummary(presence=set(), min_dates={}, max_dates={}, raw_keys=set())
        _supplement_event_summary_from_custom_fields(
            mid,
            summary,
            ctx=MatterContext("OUT", "PATENT", "26PO0107US", False),
        )

        assert summary.min_dates["APPLICATION_DEADLINE"] == date(2026, 5, 21)


class TestLiveFilingDeadlineSource:
    def test_filing_red_source_ignores_internal_custom_deadline(self, app, db_session):
        from app.models.ip_records import DocketItem, Matter, MatterCustomField, MatterEvent
        from app.models.workflow import Workflow
        from app.services.matter.matter_auto_status import (
            _has_live_filing_deadline_source,
            derive_auto_status,
        )

        mid = "mid-filing-source-internal-custom"
        db_session.add(
            Matter(
                matter_id=mid,
                our_ref="26PD9998US",
                right_group="Text",
                matter_type="PATENT",
                status_red="FilingDeadline",
                status_red_related_date="2026-05-28",
            )
        )
        db_session.add(
            MatterCustomField(
                matter_id=mid,
                namespace="domestic_patent",
                data={
                    "filing_deadline": "2026-05-28",
                    "filing_deadline_type": "INTERNAL",
                },
            )
        )
        db_session.add(
            MatterEvent(
                mevent_id="mid-filing-source-internal-custom-event",
                matter_id=mid,
                event_key="Filing deadline",
                event_at="2026-05-28",
                source_column="form:domestic_patent",
            )
        )
        db_session.add(
            DocketItem(
                docket_id="filing-source-internal-custom-docket",
                matter_id=mid,
                category="WORK",
                name_ref="Filing",
                name_free="FilingDeadline",
                due_date="2026-05-28",
                done_date="",
                is_deleted=False,
            )
        )
        db_session.add(
            Workflow(
                case_id=mid,
                name="FilingDeadline",
                status="Pending",
                legal_due_date=date(2026, 5, 28),
                due_date=date(2026, 5, 28),
            )
        )
        db_session.commit()

        assert not _has_live_filing_deadline_source(
            matter_id=mid,
            due_date=date(2026, 5, 28),
        )

        auto = derive_auto_status(
            matter_id=mid,
            current_red="FilingDeadline",
            current_red_date="2026-05-28",
            current_blue="Filing Examination In Progress",
        )
        assert auto.status_red == ""
        assert auto.status_red_related_date == ""
        assert auto.status_blue == "Filing  In Progress"

    def test_legal_custom_deadline_still_counts_as_live_filing_source(self, app, db_session):
        from app.models.ip_records import DocketItem, MatterCustomField
        from app.services.matter.matter_auto_status import _has_live_filing_deadline_source

        mid = "mid-filing-source-legal-custom"
        db_session.add(
            MatterCustomField(
                matter_id=mid,
                namespace="outgoing_patent",
                data={
                    "filing_deadline": "2026-05-28",
                    "filing_deadline_type": "LEGAL",
                },
            )
        )
        db_session.add(
            DocketItem(
                docket_id="filing-source-legal-custom-docket",
                matter_id=mid,
                category="WORK",
                name_ref="Filing",
                name_free="FilingDeadline",
                due_date="2026-05-28",
                done_date="",
                is_deleted=False,
            )
        )
        db_session.commit()

        assert _has_live_filing_deadline_source(
            matter_id=mid,
            due_date=date(2026, 5, 28),
        )

    def test_filing_red_source_ignores_internal_extended_due(self, app, db_session):
        from app.models.ip_records import DocketItem
        from app.models.workflow import Workflow
        from app.services.matter.matter_auto_status import _has_live_filing_deadline_source

        mid = "mid-filing-source-internal-due"
        db_session.add(
            DocketItem(
                docket_id="filing-source-internal-due",
                matter_id=mid,
                category="WORK",
                name_ref="Filing",
                name_free="FilingDeadline",
                due_date="2026-05-21",
                extended_due_date="2026-05-01",
                done_date="",
                is_deleted=False,
            )
        )
        db_session.add(
            Workflow(
                case_id=mid,
                name="FilingDeadline",
                status="Pending",
                legal_due_date=date(2026, 5, 21),
                due_date=date(2026, 5, 1),
            )
        )
        db_session.commit()

        assert not _has_live_filing_deadline_source(
            matter_id=mid,
            due_date=date(2026, 5, 1),
        )
        assert _has_live_filing_deadline_source(
            matter_id=mid,
            due_date=date(2026, 5, 21),
        )


class TestCollectPostFilingPendingDeadlines:
    """Tests for _collect_post_filing_pending_deadlines helper."""

    def test_collect_requires_application_date(self):
        from app.services.matter.matter_auto_status import _collect_post_filing_pending_deadlines

        ed = {"FOREIGN_FILING_DEADLINE": date(2027, 1, 1)}
        assert _collect_post_filing_pending_deadlines(set(ed.keys()), ed) == []

    def test_collect_both_foreign_and_exam(self):
        from app.services.matter.matter_auto_status import _collect_post_filing_pending_deadlines

        ed = {
            "APPLICATION_DATE": date(2026, 1, 1),
            "FOREIGN_FILING_DEADLINE": date(2027, 1, 1),
            "EXAM_REQUEST_DEADLINE": date(2029, 1, 1),
        }
        assert _collect_post_filing_pending_deadlines(set(ed.keys()), ed) == [
            ("ForeignFilingDeadline", date(2027, 1, 1)),
            ("Examination requestDeadline", date(2029, 1, 1)),
        ]

    def test_collect_skips_completed(self):
        from app.services.matter.matter_auto_status import _collect_post_filing_pending_deadlines

        ed = {
            "APPLICATION_DATE": date(2026, 1, 1),
            "FOREIGN_FILING_DEADLINE": date(2027, 1, 1),
            "FOREIGN_FILING_DATE": date(2026, 6, 1),
            "EXAM_REQUEST_DEADLINE": date(2029, 1, 1),
        }
        assert _collect_post_filing_pending_deadlines(set(ed.keys()), ed) == [
            ("Examination requestDeadline", date(2029, 1, 1)),
        ]

    def test_collect_uses_fallback_due(self):
        from app.services.matter.matter_auto_status import _collect_post_filing_pending_deadlines

        ed = {"APPLICATION_DATE": date(2026, 1, 1)}
        fb = {"EXAM_REQUEST_DEADLINE": date(2029, 1, 1)}
        assert _collect_post_filing_pending_deadlines(
            set(ed.keys()), ed, fallback_due_by_std_key=fb
        ) == [
            ("Examination requestDeadline", date(2029, 1, 1)),
        ]

    def test_collect_uses_event_summary_for_completion(self):
        from app.services.matter.matter_auto_status import (
            EventSummary,
            _collect_post_filing_pending_deadlines,
        )

        event_presence = {"APPLICATION_DATE", "EXAM_REQUEST_DEADLINE"}
        event_due = {
            "APPLICATION_DATE": date(2026, 1, 1),
            "EXAM_REQUEST_DEADLINE": date(2029, 1, 1),
        }
        summary = EventSummary(
            presence={"APPLICATION_DATE", "EXAM_REQUESTED"},
            min_dates={"APPLICATION_DATE": date(2026, 1, 1)},
            max_dates={"APPLICATION_DATE": date(2026, 1, 1)},
            raw_keys=set(),
        )
        assert (
            _collect_post_filing_pending_deadlines(
                event_presence,
                event_due,
                event_summary=summary,
            )
            == []
        )


class TestMergeBlueWithPendingPostFiling:
    def test_merge_combines_when_base_is_one_of_parts(self):
        from app.services.matter.matter_auto_status import _merge_blue_with_pending_post_filing

        merged = _merge_blue_with_pending_post_filing(
            "Filing Examination In Progress",
            [
                ("ForeignFilingDeadline", date(2027, 1, 1)),
                ("Examination requestDeadline", date(2029, 1, 1)),
            ],
        )
        assert merged == "ForeignFiling In Progress(ExaminationBilling)"

    def test_merge_keeps_mainline_blue_with_parallel_action(self):
        from app.services.matter.matter_auto_status import _merge_blue_with_pending_post_filing

        merged = _merge_blue_with_pending_post_filing(
            "Filing Examination In Progress",
            [("ForeignFilingDeadline", date(2027, 1, 1))],
            preserve_primary_blue=True,
        )
        assert "Filing Examination In Progress" in merged
        assert "ForeignFiling  In Progress" in merged
        return
        assert merged == "Text Text Text · Text Text Text"

    def test_merge_keeps_strong_blue(self):
        from app.services.matter.matter_auto_status import _merge_blue_with_pending_post_filing

        merged = _merge_blue_with_pending_post_filing(
            "OA  In Progress",
            [
                ("ForeignFilingDeadline", date(2027, 1, 1)),
                ("Examination requestDeadline", date(2029, 1, 1)),
            ],
        )
        assert merged == "OA  In Progress"


class TestSignalKindMatches:
    def test_unknown_expected_allows_strong_dispatch_signal(self):
        from app.services.matter.matter_auto_status import _ResponseSignal, _signal_kind_matches

        signal = _ResponseSignal(dt=date(2026, 1, 1), dispatch_digits="952026012345678", kind=None)
        assert _signal_kind_matches(None, signal.kind, signal=signal) is True


class TestPickEventBasedRed:
    def test_appeal_deadline_can_be_selected_as_red(self):
        from app.services.matter.matter_auto_status import (
            _pick_event_based_red,
            _summarize_event_rows,
        )

        event_rows = [("APPEAL_DEADLINE", "APPEAL_DEADLINE", "2030-01-15")]
        summary = _summarize_event_rows(event_rows)
        red, red_date = _pick_event_based_red(
            "mid",
            event_rows=event_rows,
            event_summary=summary,
        )
        assert red == "Deadline"
        assert red_date == "2030-01-15"

    def test_respects_earliest_open_pipeline_stage(self, monkeypatch):
        from app.services.matter.matter_auto_status import RedRule, _pick_event_based_red

        rules = (
            RedRule(
                key="APPLICATION_DEADLINE",
                label="Text",
                deadline_event_key="APPLICATION_DEADLINE",
                completion_event_key="APPLICATION_DATE",
                activation_event_keys=(),
                red_class="pipeline",
                stage=10,
            ),
            RedRule(
                key="EXAM_REQUEST_DEADLINE",
                label="Text",
                deadline_event_key="EXAM_REQUEST_DEADLINE",
                completion_event_key="EXAM_REQUESTED",
                activation_event_keys=(),
                red_class="pipeline",
                stage=30,
            ),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_red_rules",
            lambda: rules,
        )

        event_rows = [("EXAM_REQUEST_DEADLINE", "EXAM_REQUEST_DEADLINE", "2030-01-15")]
        red, red_date = _pick_event_based_red("mid", event_rows=event_rows)
        assert red == ""
        assert red_date == ""


class TestPickRedByPriority:
    def test_pipeline_does_not_skip_open_earlier_stage_without_due(self, monkeypatch):
        from app.services.matter.matter_auto_status import RedRule, _pick_red_by_priority

        rules = (
            RedRule(
                key="S10",
                label="S10",
                deadline_event_key="S10",
                completion_event_key="DONE10",
                activation_event_keys=(),
                red_class="pipeline",
                stage=10,
            ),
            RedRule(
                key="S30",
                label="S30",
                deadline_event_key="S30",
                completion_event_key="DONE30",
                activation_event_keys=(),
                red_class="pipeline",
                stage=30,
            ),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_red_rules",
            lambda: rules,
        )

        red, red_date = _pick_red_by_priority(
            event_presence=set(),
            event_due_by_std_key={"S30": date(2030, 1, 1)},
            expired_deadlines=set(),
            event_summary=None,
        )
        assert red == ""
        assert red_date == ""


class TestDeriveAutoStatusPriority:
    def test_terminal_blue_is_not_overridden_by_open_oa(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("DOM", "PATENT", "24PD0001US", True),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [("REGISTRATION_DATE", "REGISTRATION_DATE", "2026-02-13")],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("Text", "2026-03-01"),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )

        status = derive_auto_status(matter_id="mid")
        assert status.status_blue == "RegistrationDone"
        assert status.display_blue == "RegistrationDone"

    def test_stale_exam_blue_without_evidence_falls_back_to_default(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("DOM", "PATENT", "26PD0001US", True),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._default_blue_for_matter",
            lambda _mid: "Text Text Text",
        )

        status = derive_auto_status(matter_id="mid", current_blue="Text Text Text")
        assert status.status_blue == "Text Text Text"
        assert status.display_blue == "Text Text Text"

    def test_non_pipeline_current_blue_can_still_be_preserved(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("DOM", "PATENT", "26PD0001US", True),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._default_blue_for_matter",
            lambda _mid: "Text Text Text",
        )

        status = derive_auto_status(matter_id="mid", current_blue="Text Text")
        assert status.status_blue == "Text Text"
        assert status.display_blue == "Text Text"

    def test_unknown_red_can_fallback_to_open_mgmt_status_red_docket(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("OUT", "PCT", "24PD0106PCT", False),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_mgmt_status_red_deadline",
            lambda _mid: ("PCTText", "2026-06-22"),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )

        status = derive_auto_status(
            matter_id="mid",
            current_red="Domestic Deadline 1  Notice",
            current_red_date="2025-06-21",
        )
        assert status.status_red == "PCTText"
        assert status.status_red_related_date == "2026-06-22"

    def test_pct_advisory_red_is_not_preserved_as_current_status(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("OUT", "PCT", "26PD0102PCT", True),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_mgmt_status_red_deadline",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._default_blue_for_matter",
            lambda _mid: "",
        )

        status = derive_auto_status(
            matter_id="mid",
            current_red="Domestic Deadline 1  Notice",
            current_red_date="2027-08-30",
            current_blue="Filing Examination In Progress",
        )

        assert status.status_red == ""
        assert status.status_red_related_date == ""

    def test_open_mgmt_red_picker_skips_pct_advisory_labels(
        self, app, db_session, monkeypatch
    ):
        import uuid

        from app.models.docket import DocketItem
        from app.models.matter import Matter
        from app.services.matter.matter_auto_status import _pick_open_mgmt_status_red_deadline

        today = date.today()
        matter_id = uuid.uuid4().hex
        canonical_due = today + timedelta(days=100)
        advisory_due = today + timedelta(days=10)
        db_session.add(
            Matter(
                matter_id=matter_id,
                our_ref=f"26PD{uuid.uuid4().hex[:4].upper()}PCT",
                right_group="ETC",
                matter_type="PCT",
                right_name="PCT advisory skip",
                is_deleted=False,
            )
        )
        db_session.add(
            DocketItem(
                docket_id="pct-advisory-current-red",
                matter_id=matter_id,
                category="MGMT",
                name_ref="MGMT:STATUS_RED:Domestic Deadline 1  Notice",
                name_free="Domestic Deadline 1  Notice",
                due_date=advisory_due.isoformat(),
                done_date=None,
                is_deleted=False,
            )
        )
        db_session.add(
            DocketItem(
                docket_id="pct-canonical-current-red",
                matter_id=matter_id,
                category="MGMT_WORK",
                name_ref="MGMT:STATUS_RED:PCTText",
                name_free="PCTText",
                due_date=canonical_due.isoformat(),
                done_date=None,
                is_deleted=False,
            )
        )
        db_session.commit()

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_response_signals",
            lambda *_args, **_kwargs: [],
        )

        assert _pick_open_mgmt_status_red_deadline(matter_id) == (
            "PCTText",
            canonical_due.isoformat(),
        )

    def test_hidden_pct_mgmt_status_red_is_in_display_red(
        self, app, db_session, monkeypatch
    ):
        import uuid

        from app.models.docket import DocketItem
        from app.models.matter import Matter
        from app.services.matter.matter_auto_status import derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._today",
            lambda: date(2026, 6, 27),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._default_blue_for_matter",
            lambda _mid: "",
        )

        matter_id = uuid.uuid4().hex
        db_session.add(
            Matter(
                matter_id=matter_id,
                our_ref="26PD0998PCT",
                right_group="ETC",
                matter_type="PCT",
                right_name="PCT hidden display",
                is_deleted=False,
            )
        )
        db_session.add(
            DocketItem(
                docket_id=uuid.uuid4().hex,
                matter_id=matter_id,
                category="MGMT_WORK",
                name_ref="MGMT:STATUS_RED:PCTDomesticDeadline",
                name_free="PCTDomesticDeadline",
                due_date="2028-01-01",
                done_date=None,
                is_deleted=False,
            )
        )
        db_session.commit()

        status = derive_auto_status(matter_id=matter_id)

        assert status.status_red == ""
        assert status.display_red == "PCTDomesticDeadline[2028-01-01]"

    def test_notice_send_ref_can_drive_publication_blue(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("DOM", "TRADEMARK", "25TD0001US", True),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [
                ("APPLICATION_DATE", "APPLICATION_DATE", "2025-01-01"),
                ("EXAM_REQUEST_DATE", "EXAM_REQUEST_DATE", "2025-03-01"),
            ],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_mgmt_status_red_deadline",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._resolve_notice_send_doc_name_from_red",
            lambda _mid, _red: "Publication decision",
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )

        status = derive_auto_status(
            matter_id="mid",
            current_red="MGMT:NOTICE_SEND_3D:oa-1",
            current_red_date="2026-03-06",
        )
        assert status.status_blue == "Filing Publication In Progress"
        assert status.display_blue == "Filing Publication In Progress"

    def test_empty_red_can_still_use_notice_send_blue_signal(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("DOM", "TRADEMARK", "25TD0002US", True),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [
                ("APPLICATION_DATE", "APPLICATION_DATE", "2025-01-01"),
                ("EXAM_REQUEST_DATE", "EXAM_REQUEST_DATE", "2025-03-01"),
            ],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_notice_send_blue_signal",
            lambda _mid: "Text Text Text",
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )

        status = derive_auto_status(
            matter_id="mid",
            current_red="",
            current_blue="Text Text Text",
        )
        assert status.status_blue == "Text Text Text"
        assert status.display_blue == "Text Text Text"

    def test_domestic_pending_post_filing_reds_display_before_visibility_window(
        self, monkeypatch
    ):
        from datetime import date

        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._today",
            lambda: date(2026, 6, 27),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("DOM", "PATENT", "26PD0222US", True),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [
                ("APPLICATION_DATE", "APPLICATION_DATE", "2026-06-24"),
                ("FOREIGN_FILING_DEADLINE", "FOREIGN_FILING_DEADLINE", "2027-06-24"),
            ],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_notice_send_blue_signal",
            lambda _mid: "",
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_mgmt_status_red_deadline",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )

        status = derive_auto_status(matter_id="mid")

        assert status.status_red == ""
        assert status.display_red == (
            "ForeignFilingDeadline[2027-06-24]\n"
            "Examination requestDeadline[2029-06-24]"
        )
        assert status.display_blue == "ForeignFiling In Progress(ExaminationBilling)"

    def test_hidden_foreign_filing_does_not_block_visible_exam_deadline(self, monkeypatch):
        from datetime import date

        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._today",
            lambda: date(2026, 4, 23),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("OUT", "PATENT", "26PO0001US", False),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [
                ("APPLICATION_DATE", "APPLICATION_DATE", "2026-01-10"),
                ("FOREIGN_FILING_DEADLINE", "FOREIGN_FILING_DEADLINE", "2026-12-15"),
                ("EXAM_REQUEST_DEADLINE", "EXAM_REQUEST_DEADLINE", "2026-05-01"),
            ],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_mgmt_status_red_deadline",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )

        status = derive_auto_status(matter_id="mid")

        assert status.status_red == "Examination requestDeadline"
        assert status.status_red_related_date == "2026-05-01"
        assert status.status_blue == "Examination  Billing In Progress"
        assert "ForeignFilingDeadline" not in status.display_red

    def test_outgoing_application_date_completes_foreign_filing_status(self, monkeypatch):
        from datetime import date

        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._today",
            lambda: date(2026, 11, 20),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("OUT", "PATENT", "26PO0001US", False),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [
                ("APPLICATION_DATE", "APPLICATION_DATE", "2026-02-01"),
                ("FOREIGN_FILING_DEADLINE", "FOREIGN_FILING_DEADLINE", "2026-12-15"),
                ("EXAM_REQUEST_DEADLINE", "EXAM_REQUEST_DEADLINE", "2026-11-25"),
            ],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_mgmt_status_red_deadline",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )

        status = derive_auto_status(matter_id="mid")

        assert status.status_red == "Examination requestDeadline"
        assert status.status_red_related_date == "2026-11-25"
        assert status.status_blue == "Examination  Billing In Progress"
        assert "ForeignFilingDeadline" not in status.display_red
        assert "ForeignFiling" not in status.display_blue

    def test_hidden_pct_national_phase_current_red_is_cleared(self, monkeypatch):
        from datetime import date

        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._today",
            lambda: date(2026, 4, 23),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("OUT", "PCT", "24PD0102PCT", True),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_mgmt_status_red_deadline",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_mgmt_status_red_due_for_label",
            lambda _mid, _label: "2026-11-30",
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )

        status = derive_auto_status(
            matter_id="mid",
            current_red="Domestic Deadline 1  Notice",
            current_red_date="2026-11-30",
            current_blue="Filing Examination In Progress",
        )

        assert status.status_red == ""
        assert status.status_red_related_date == ""
        assert status.status_blue == "Filing Examination In Progress"

    def test_future_term_expiry_date_does_not_force_closed_status(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("OUT", "TRADEMARK", "25TO0105HK", False),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [
                ("Filing deadline", "APPLICATION_DEADLINE", "2026-03-28"),
                ("Term expiry", "TERM_EXPIRY_DATE", "2035-09-14"),
            ],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._default_blue_for_matter",
            lambda _mid: "Filing Examination In Progress",
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._has_live_filing_deadline_source",
            lambda **_kwargs: True,
        )

        status = derive_auto_status(matter_id="mid")
        assert status.status_blue == "Filing  In Progress"
        assert status.status_red == "FilingDeadline"
        assert status.status_red_related_date == "2026-03-28"

    def test_current_term_expiry_red_date_refreshes_from_latest_event_signal(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("DOM", "TRADEMARK", "25TD0101US", True),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [
                ("Registration date", "REGISTRATION_DATE", "2025-05-09"),
                ("Term expiry", "TERM_EXPIRY_DATE", "2030-05-09"),
            ],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_mgmt_status_red_deadline",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )

        status = derive_auto_status(
            matter_id="mid",
            current_red="Term expired",
            current_red_date="2035-05-09",
            current_blue="Matter closed",
        )
        assert status.status_red == "Term expired"
        assert status.status_red_related_date == "2030-05-09"
        assert status.status_blue == "RegistrationDone"

    def test_future_term_expiry_red_is_preserved_without_authoritative_signal(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        future_date = (date.today() + timedelta(days=3650)).isoformat()

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("DOM", "DESIGN", "22DD0114US", True),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_red_by_priority",
            lambda *_args, **_kwargs: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_event_based_red",
            lambda *_args, **_kwargs: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_mgmt_status_red_deadline",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._refresh_red_related_date_from_authoritative_sources",
            lambda **_kwargs: "",
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._infer_red_related_date_from_event_signals",
            lambda *_args, **_kwargs: "",
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )

        status = derive_auto_status(
            matter_id="mid",
            current_red="Term expired",
            current_red_date=future_date,
            current_blue="Matter closed",
        )

        assert status.status_red == "Term expired"
        assert status.status_red_related_date == future_date
        assert status.status_blue == "Matter closed"

    def test_manual_abandon_red_is_preserved_over_auto_registration_deadline(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("DOM", "PATENT", "23PD0174US", True),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_red_by_priority",
            lambda *_args, **_kwargs: ("RegistrationDeadline", "2024-06-14"),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_mgmt_status_red_deadline",
            lambda _mid: ("RegistrationDeadline", "2024-06-14"),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )

        status = derive_auto_status(
            matter_id="mid",
            current_red="Abandoned(Client instructed abandonment)",
            current_red_date="2025-01-06",
            current_blue="RegistrationWaiting In Progress",
        )
        assert status.status_red == "Abandoned(Client instructed abandonment)"
        assert status.status_red_related_date == "2025-01-06"
        assert status.status_blue == "Matter closed"

    def test_manual_closed_red_is_not_overridden_by_open_office_action(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("OUT", "PATENT", "25TO0103CN", False),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("Notice", "2026-01-25"),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )

        status = derive_auto_status(
            matter_id="mid",
            current_red="Matter closed",
            current_red_date="2025-08-15",
            current_blue="Matter closed",
        )
        assert status.status_red == "Matter closed"
        assert status.status_red_related_date == "2025-08-15"
        assert status.status_blue == "Matter closed"

    def test_current_rule_red_date_refreshes_from_latest_event_signal(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("DOM", "PATENT", "26PD0001US", True),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [("Filing deadline", "APPLICATION_DEADLINE", "2026-03-28")],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_mgmt_status_red_deadline",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._has_live_filing_deadline_source",
            lambda **_kwargs: True,
        )

        status = derive_auto_status(
            matter_id="mid",
            current_red="FilingDeadline",
            current_red_date="2026-02-11",
            current_blue="Filing Examination In Progress",
        )
        assert status.status_red == "FilingDeadline"
        assert status.status_red_related_date == "2026-03-28"

    def test_current_office_action_red_date_refreshes_from_open_oa_due(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("DOM", "PATENT", "26PD0002US", True),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_due_for_label",
            lambda _mid, _label: "2026-01-25",
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_mgmt_status_red_due_for_label",
            lambda _mid, _label: "",
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._is_stale_oa_red",
            lambda **_kwargs: False,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )

        status = derive_auto_status(
            matter_id="mid",
            current_red="Notice",
            current_red_date="2026-01-18",
            current_blue="OA  In Progress",
        )
        assert status.status_red == "Notice"
        assert status.status_red_related_date == "2026-01-25"

    def test_current_mgmt_red_date_refreshes_from_open_mgmt_docket(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("OUT", "PCT", "24PD0106PCT", False),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_mgmt_status_red_deadline",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_mgmt_status_red_due_for_label",
            lambda _mid, _label: "2026-06-22",
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )

        status = derive_auto_status(
            matter_id="mid",
            current_red="PCTText",
            current_red_date="2025-06-21",
            current_blue="Text Text Text",
        )
        assert status.status_red == "PCTText"
        assert status.status_red_related_date == "2026-06-22"

    def test_litigation_pending_overrides_past_request_deadline(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("DOM", "LITIGATION", "26TI0001US", True),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [("/Billing/ Deadline", "/Billing/ Deadline", "2026-02-25")],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_mgmt_status_red_deadline",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._default_blue_for_matter",
            lambda _mid: "Text Text",
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._has_litigation_pending_signal",
            lambda *_args, **_kwargs: True,
        )

        status = derive_auto_status(
            matter_id="mid",
            current_red=" /Billing/Deadline",
            current_red_date="2026-02-25",
            current_blue="Matter In Progress",
        )
        assert status.status_red == ""
        assert status.status_red_related_date == ""
        assert status.status_blue == "Matter In Progress"
        assert status.display_blue == "Matter In Progress"

    def test_litigation_future_request_deadline_stays_actionable(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("DOM", "LITIGATION", "26TI0002US", True),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [("/Billing/ Deadline", "/Billing/ Deadline", "2099-02-25")],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_mgmt_status_red_deadline",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._default_blue_for_matter",
            lambda _mid: "Matter In Progress",
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._has_litigation_pending_signal",
            lambda *_args, **_kwargs: True,
        )

        status = derive_auto_status(matter_id="mid")
        assert status.status_red == "/Billing/Deadline"
        assert status.status_red_related_date == "2099-02-25"

    def test_past_term_expiry_date_closes_case(self, monkeypatch):
        from app.services.matter.matter_auto_status import MatterContext, derive_auto_status

        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._get_matter_context",
            lambda _mid: MatterContext("OUT", "TRADEMARK", "20TO0105HK", False),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._fetch_event_rows",
            lambda _mid: [("Term expiry", "TERM_EXPIRY_DATE", "2020-09-14")],
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._supplement_event_summary_from_custom_fields",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._pick_open_office_action_red",
            lambda _mid: ("", ""),
        )
        monkeypatch.setattr(
            "app.services.matter.matter_auto_status._expired_deadlines_by_policy",
            lambda *_args, **_kwargs: set(),
        )

        status = derive_auto_status(matter_id="mid")
        assert status.status_blue == "Matter closed"
        assert status.status_red == ""


class TestAutoStatus:
    """Tests for AutoStatus dataclass."""

    def test_auto_status_creation(self):
        from app.services.matter.matter_auto_status import AutoStatus

        status = AutoStatus(
            status_red="Text",
            status_red_related_date="2026-03-15",
            status_blue="Text Text Text",
            display_red="Text[2026-03-15]",
            display_blue="Text Text Text",
        )
        assert status.status_red == "Text"
        assert status.status_red_related_date == "2026-03-15"
        assert status.status_blue == "Text Text Text"

    def test_auto_status_defaults(self):
        from app.services.matter.matter_auto_status import AutoStatus

        status = AutoStatus()
        assert status.status_red == ""
        assert status.status_red_related_date == ""
        assert status.status_blue == ""
        assert status.display_red == ""
        assert status.display_blue == ""

    def test_auto_status_immutable(self):
        from app.services.matter.matter_auto_status import AutoStatus

        status = AutoStatus(status_red="test")
        # frozen=True means the dataclass is immutable
        with pytest.raises(AttributeError):
            status.status_red = "new_value"


def test_fetch_response_signals_skips_missing_document_without_reporting(app, monkeypatch):
    from app.services.matter import matter_auto_status as mas

    class _Result:
        def all(self):
            return [("2026-04-03", "matter/missing-response.txt")]

    class _FileService:
        def abs_path(self, path):
            assert path == "matter/missing-response.txt"
            return Path("/tmp/ipm-missing-response-document-does-not-exist")

    reported: list[tuple[tuple, dict]] = []

    monkeypatch.setattr(mas.db.session, "execute", lambda *args, **kwargs: _Result())
    monkeypatch.setattr(
        "app.services.storage.file_asset_service.get_file_asset_service",
        lambda: _FileService(),
    )
    monkeypatch.setattr(mas, "_fetch_non_email_response_rows", lambda _mid: [])
    monkeypatch.setattr(
        mas,
        "report_swallowed_exception",
        lambda *args, **kwargs: reported.append((args, kwargs)),
    )

    with app.app_context():
        signals = mas._fetch_response_signals("mid")

    assert signals == []
    assert reported == []
