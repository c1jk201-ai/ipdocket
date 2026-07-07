"""
Unit tests for mgmt_deadlines service.

Includes focused DB-backed regressions for session recovery helpers.
"""

from datetime import date, datetime

import pytest


class TestParseDate:
    """Tests for _parse_date function."""

    def test_parse_date_none(self):
        from app.services.deadlines.mgmt_deadlines import _parse_date

        assert _parse_date(None) is None

    def test_parse_date_empty_string(self):
        from app.services.deadlines.mgmt_deadlines import _parse_date

        assert _parse_date("") is None
        assert _parse_date("   ") is None

    def test_parse_date_iso_format(self):
        from app.services.deadlines.mgmt_deadlines import _parse_date

        result = _parse_date("2026-01-15")
        assert result == date(2026, 1, 15)

    def test_parse_date_datetime_object(self):
        from app.services.deadlines.mgmt_deadlines import _parse_date

        dt = datetime(2026, 3, 20, 10, 30)
        result = _parse_date(dt)
        assert result == date(2026, 3, 20)

    def test_parse_date_date_object(self):
        from app.services.deadlines.mgmt_deadlines import _parse_date

        d = date(2026, 5, 1)
        result = _parse_date(d)
        assert result == date(2026, 5, 1)

    def test_parse_date_with_brackets(self):
        from app.services.deadlines.mgmt_deadlines import _parse_date

        result = _parse_date("[2026-06-30]")
        assert result == date(2026, 6, 30)

    def test_parse_date_with_timestamp(self):
        from app.services.deadlines.mgmt_deadlines import _parse_date

        result = _parse_date("2026-07-15T14:30:00")
        assert result == date(2026, 7, 15)

    def test_parse_date_embedded_in_text(self):
        from app.services.deadlines.mgmt_deadlines import _parse_date

        result = _parse_date("some text 2026-08-20 more text")
        assert result == date(2026, 8, 20)

    def test_parse_date_invalid_format(self):
        from app.services.deadlines.mgmt_deadlines import _parse_date

        assert _parse_date("invalid") is None
        assert _parse_date("not-a-date") is None
        assert _parse_date("222222-02-02") is None


class TestApplyOffset:
    """Tests for _apply_offset function."""

    def test_apply_offset_days(self):
        from app.services.deadlines.mgmt_deadlines import _apply_offset

        base = date(2026, 1, 1)
        result = _apply_offset(base, days=10)
        assert result == date(2026, 1, 11)

    def test_apply_offset_negative_days(self):
        from app.services.deadlines.mgmt_deadlines import _apply_offset

        base = date(2026, 1, 15)
        result = _apply_offset(base, days=-10)
        assert result == date(2026, 1, 5)

    def test_apply_offset_months(self):
        from app.services.deadlines.mgmt_deadlines import _apply_offset

        base = date(2026, 1, 15)
        result = _apply_offset(base, months=3)
        assert result == date(2026, 4, 15)

    def test_apply_offset_negative_months(self):
        from app.services.deadlines.mgmt_deadlines import _apply_offset

        base = date(2026, 6, 15)
        result = _apply_offset(base, months=-3)
        assert result == date(2026, 3, 15)

    def test_apply_offset_years(self):
        from app.services.deadlines.mgmt_deadlines import _apply_offset

        base = date(2026, 5, 20)
        result = _apply_offset(base, years=2)
        assert result == date(2028, 5, 20)

    def test_apply_offset_combined(self):
        from app.services.deadlines.mgmt_deadlines import _apply_offset

        base = date(2026, 1, 1)
        result = _apply_offset(base, days=10, months=2, years=1)
        assert result == date(2027, 3, 11)


class TestParseMemo:
    """Tests for _parse_memo function."""

    def test_parse_memo_none(self):
        from app.services.deadlines.mgmt_deadlines import _parse_memo

        assert _parse_memo(None) == {}

    def test_parse_memo_empty_string(self):
        from app.services.deadlines.mgmt_deadlines import _parse_memo

        assert _parse_memo("") == {}

    def test_parse_memo_valid_json(self):
        from app.services.deadlines.mgmt_deadlines import _parse_memo

        result = _parse_memo('{"key": "value", "locked": true}')
        assert result == {"key": "value", "locked": True}

    def test_parse_memo_invalid_json(self):
        from app.services.deadlines.mgmt_deadlines import _parse_memo

        assert _parse_memo("not json") == {}

    def test_parse_memo_json_array(self):
        from app.services.deadlines.mgmt_deadlines import _parse_memo

        # Arrays should return empty dict (not a dict type)
        assert _parse_memo('["a", "b"]') == {}


class TestValidateTemplate:
    """Tests for _validate_template function."""

    def test_validate_template_missing_trigger(self):
        from app.services.deadlines.mgmt_deadlines import _validate_template

        is_valid, error = _validate_template({"id": "test"})
        assert not is_valid
        assert "missing 'trigger' field" in error

    def test_validate_template_unknown_trigger(self):
        from app.services.deadlines.mgmt_deadlines import _validate_template

        is_valid, error = _validate_template({"id": "test", "trigger": "unknown_trigger"})
        assert not is_valid
        assert "unknown trigger type" in error

    def test_validate_template_deadline_code_missing_code(self):
        from app.services.deadlines.mgmt_deadlines import _validate_template

        is_valid, error = _validate_template({"id": "test", "trigger": "deadline_code"})
        assert not is_valid
        assert "missing required field 'deadline_code'" in error

    def test_validate_template_invalid_offset(self):
        from app.services.deadlines.mgmt_deadlines import _validate_template

        is_valid, error = _validate_template(
            {"id": "test", "trigger": "office_action_received", "offset_days": "not_a_number"}
        )
        assert not is_valid
        assert "must be an integer" in error

    def test_validate_template_valid(self):
        from app.services.deadlines.mgmt_deadlines import _validate_template

        is_valid, error = _validate_template(
            {
                "id": "test",
                "trigger": "deadline_code",
                "deadline_code": "FOREIGN_FILING",
                "offset_days": -30,
            }
        )
        assert is_valid
        assert error == ""


class TestNormalizeTemplate:
    """Tests for _normalize_template function."""

    def test_normalize_template_defaults(self):
        from app.services.deadlines.mgmt_deadlines import _normalize_template

        result = _normalize_template({"trigger": "test"})
        assert result["id"] == ""
        assert result["category"] == "MGMT"
        assert result["title"] == ""
        assert result["offset_days"] == 0
        assert result["offset_months"] == 0
        assert result["offset_years"] == 0

    def test_normalize_template_strips_whitespace(self):
        from app.services.deadlines.mgmt_deadlines import _normalize_template

        result = _normalize_template(
            {"id": "  test_id  ", "trigger": " deadline_code ", "title": "  Some Title  "}
        )
        assert result["id"] == "test_id"
        assert result["trigger"] == "deadline_code"
        assert result["title"] == "Some Title"


class TestResolveAssigneeValue:
    """Tests for _resolve_assignee_value function."""

    def test_resolve_assignee_value_none_field(self):
        from app.services.deadlines.mgmt_deadlines import _resolve_assignee_value

        result = _resolve_assignee_value({"manager": "John"}, None)
        assert result is None

    def test_resolve_assignee_value_missing_field(self):
        from app.services.deadlines.mgmt_deadlines import _resolve_assignee_value

        result = _resolve_assignee_value({"manager": "John"}, "attorney")
        assert result is None

    def test_resolve_assignee_value_found(self):
        from app.services.deadlines.mgmt_deadlines import _resolve_assignee_value

        result = _resolve_assignee_value({"manager": "  John Doe  "}, "manager")
        assert result == "John Doe"

    def test_resolve_assignee_value_empty_value(self):
        from app.services.deadlines.mgmt_deadlines import _resolve_assignee_value

        result = _resolve_assignee_value({"manager": "   "}, "manager")
        assert result is None


def test_policy_matches_prefix_ignores_whitespace():
    from app.services.deadlines.mgmt_deadlines import _policy_matches

    policy = {"id": "PFX", "match": {"name_ref_prefixes": ["MGMT:STATUS_RED:"]}}
    assert (
        _policy_matches(
            policy,
            deadline_code=None,
            name_ref="MGMT: STATUS_RED:Text",
            template_id=None,
        )
        is True
    )


def test_policy_matches_invalid_regex_does_not_raise():
    from app.services.deadlines.mgmt_deadlines import _policy_matches

    policy = {"id": "REGEX_BAD", "match": {"name_ref_regexes": ["(["]}}
    assert (
        _policy_matches(
            policy,
            deadline_code=None,
            name_ref="MGMT:STATUS_RED:Text",
            template_id=None,
        )
        is False
    )


def test_normalize_deadline_policy_drops_invalid_regex():
    from app.services.deadlines.mgmt_deadlines import _normalize_deadline_policy

    policy = {
        "id": "REGEX_FILTER",
        "match": {"name_ref_regexes": ["([", "^MGMT:STATUS_RED:"]},
    }
    normalized = _normalize_deadline_policy(policy)

    assert normalized is not None
    assert normalized["match"]["name_ref_regexes"] == ["^MGMT:STATUS_RED:"]


def test_non_action_status_red_labels_are_not_deadline_work():
    from app.utils.status_red_visibility import is_non_action_status_red_label

    assert is_non_action_status_red_label("ExaminationWaiting") is True
    assert is_non_action_status_red_label("Examination In Progress") is True
    assert is_non_action_status_red_label("Custom Waiting") is True
    assert is_non_action_status_red_label("FilingDeadline") is False
    assert is_non_action_status_red_label("Notice") is False
    assert is_non_action_status_red_label("RegistrationDeadline") is False


def test_ensure_mgmt_deadlines_skips_passive_status_red(app, db_session):
    import uuid

    from app.models.ip_records import DocketItem, Matter
    from app.services.deadlines.mgmt_deadlines import ensure_mgmt_deadlines_for_matter

    matter_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=f"26PD{uuid.uuid4().hex[:4].upper()}US",
            right_name="Text Text Text",
            right_group="DOM",
            matter_type="PATENT",
            status_red="Filing Examination In Progress",
            status_red_related_date="2026-05-07",
            status_blue="Filing Examination In Progress",
            is_deleted=False,
        )
    )
    db_session.commit()

    ensure_mgmt_deadlines_for_matter(matter_id, commit=False)

    row = DocketItem.query.filter_by(
        matter_id=matter_id,
        name_ref="MGMT:STATUS_RED:Filing Examination In Progress",
    ).first()
    assert row is None


def test_merge_custom_fields_heals_pending_rollback(app, db_session, sample_matter):
    from sqlalchemy.exc import IntegrityError

    from app.models.matter import MatterCustomField
    from app.models.user import User
    from app.services.deadlines.mgmt_deadlines import _merge_custom_fields

    matter_id = str(sample_matter.matter_id)
    db_session.add(
        MatterCustomField(
            matter_id=matter_id,
            namespace="domestic_patent",
            data={"manager": "Alice"},
        )
    )
    db_session.commit()

    db_session.add(
        User(
            username=f"mgmt-heal-a-{matter_id[:8]}",
            email="mgmt-heal@example.com",
            role="user",
            is_active=True,
        )
    )
    db_session.commit()

    db_session.add(
        User(
            username=f"mgmt-heal-b-{matter_id[:8]}",
            email="mgmt-heal@example.com",
            role="user",
            is_active=True,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()

    assert bool(getattr(db_session, "is_active", False)) is False

    merged = _merge_custom_fields(matter_id)

    assert merged.get("manager") == "Alice"
    assert bool(getattr(db_session, "is_active", False)) is True


def test_resolve_owner_retries_after_invalidated_connection(
    app, db_session, sample_matter, monkeypatch
):
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.orm import Query

    from app.extensions import db
    from app.models.matter import MatterStaffAssignment
    from app.services.deadlines.mgmt_deadlines import _resolve_owner_from_matter_staff

    class DummyOrig(Exception):
        pass

    expected = MatterStaffAssignment.query.filter_by(matter_id=str(sample_matter.matter_id)).first()
    assert expected is not None

    original_all = Query.all
    original_rollback = db.session.rollback
    calls = {"all": 0, "rollback": 0}

    def fake_all(query, *args, **kwargs):
        statement = str(query.statement).lower()
        if "matter_staff_assignment" in statement and calls["all"] == 0:
            calls["all"] += 1
            raise DBAPIError(
                "SELECT staff_role_code, staff_party_id FROM matter_staff_assignment",
                {"matter_id": str(sample_matter.matter_id)},
                DummyOrig("server closed the connection unexpectedly"),
                connection_invalidated=True,
            )
        return original_all(query, *args, **kwargs)

    def fake_rollback():
        calls["rollback"] += 1
        return original_rollback()

    with app.app_context():
        monkeypatch.setattr(Query, "all", fake_all)
        monkeypatch.setattr(db.session, "rollback", fake_rollback)

        resolved = _resolve_owner_from_matter_staff(str(sample_matter.matter_id))

    assert resolved == expected.staff_party_id
    assert calls["all"] == 1
    assert calls["rollback"] == 1
