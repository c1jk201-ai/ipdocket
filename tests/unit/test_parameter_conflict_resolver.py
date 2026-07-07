"""
Unit tests for Parameter Conflict Resolver

Tests detecting conflicts between existing matter data and extracted parameters.
"""

import pytest

from app.models.ip_records import Matter, MatterCustomField
from app.services.parameter_conflict.parameter_conflict_resolver import (
    ConflictItem,
    ParameterConflictResolver,
)


def test_conflict_detection_no_conflicts(sample_matter):
    """Test when new data matches existing data or fills empty fields"""
    resolver = ParameterConflictResolver(sample_matter.matter_id)

    # extracted params matching current data
    params = {
        "our_ref": sample_matter.our_ref,
        "right_name": sample_matter.right_name,
        # events that don't exist yet
        "events": [{"event_key": "APP_DATE", "event_at": "2026-01-01"}],
    }

    result = resolver.detect_conflicts(params)

    assert len(result.conflicts) == 0
    assert len(result.auto_apply) > 0  # Should include APP_DATE

    # Verify auto-apply item
    app_date_item = next(i for i in result.auto_apply if i.field_key == "APP_DATE")
    assert app_date_item.new_value == "2026-01-01"


def test_conflict_detection_with_conflicts(sample_matter, db_session):
    """Test when new data conflicts with existing data"""
    resolver = ParameterConflictResolver(sample_matter.matter_id)

    # Extracted params with different right_name
    params = {
        "our_ref": sample_matter.our_ref,
        "right_name": "Text Text Text",  # Conflict
    }

    result = resolver.detect_conflicts(params)

    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.field_name == "right_name"
    assert conflict.current_value == sample_matter.right_name
    assert conflict.new_value == "Text Text Text"


def test_apply_auto_parameters(sample_matter, db_session):
    """Test applying auto-apply parameters"""
    resolver = ParameterConflictResolver(sample_matter.matter_id)

    # 1. Setup extract params
    params = {
        "our_ref": sample_matter.our_ref,
        "identifiers": [{"id_type": "APP_NO", "id_value": "10-2026-1234567"}],
        "events": [
            {"event_key": "APP_DATE", "event_at": "2026-01-01"},
            {"event_key": "EXAM_REQ", "raw_text": "Text"},
        ],
    }

    # 2. Detect
    result = resolver.detect_conflicts(params)
    assert len(result.conflicts) == 0
    keys = {(i.table_name, i.field_key) for i in result.auto_apply}
    assert ("matter_identifier", "APP_NO") in keys
    assert ("matter_event", "APP_DATE") in keys
    assert ("matter_event", "EXAM_REQ") in keys

    # 3. Apply
    resolver.apply_parameters(result.auto_apply, {})
    db_session.commit()

    # 4. Verify in DB
    # Check identifier
    from app.utils.policy_sql import policy_text as text

    row = db_session.execute(
        text("SELECT id_value FROM matter_identifier WHERE matter_id=:mid AND id_type='APP_NO'"),
        {"mid": sample_matter.matter_id},
    ).scalar()
    assert row == "10-2026-1234567"

    # Check event
    row = db_session.execute(
        text("SELECT raw_text FROM matter_event WHERE matter_id=:mid AND event_key='EXAM_REQ'"),
        {"mid": sample_matter.matter_id},
    ).scalar()
    assert row == "Text"

    row = (
        db_session.execute(
            text(
                "SELECT event_at, event_date FROM matter_event "
                "WHERE matter_id=:mid AND event_key='APP_DATE'"
            ),
            {"mid": sample_matter.matter_id},
        )
        .mappings()
        .first()
    )
    assert row["event_at"] == "2026-01-01"
    assert str(row["event_date"]) == "2026-01-01"


def test_party_role_conflict(sample_matter, db_session):
    """Test party role conflicts (only conflict if not in list)"""
    # Pre-seed a party role
    import uuid

    from app.utils.policy_sql import policy_text as text

    db_session.execute(
        text(
            "INSERT INTO matter_party_role (mpr_id, matter_id, role_code, raw_text) VALUES (:id, :mid, 'APPLICANT', 'Existing Applicant')"
        ),
        {"id": uuid.uuid4().hex, "mid": sample_matter.matter_id},
    )
    db_session.commit()

    resolver = ParameterConflictResolver(sample_matter.matter_id)

    # Case 1: Same applicant -> No conflict
    params1 = {"party_roles": [{"role_code": "APPLICANT", "raw_text": "Existing Applicant"}]}
    result1 = resolver.detect_conflicts(params1)
    # The logic says: if new_val in current_list, it's neither conflict nor auto-apply (it's skipped/ignored effectively)
    # Actually current implementation might not put it in either, let's check code
    # Code: if current and new_val not in current_list -> conflict
    #       elif new_val and new_val not in (current_list or []) -> auto_apply
    # So if it IS in current list, it goes nowhere.
    assert len(result1.conflicts) == 0
    assert len(result1.auto_apply) == 0

    # Case 2: New applicant -> Conflict (policy: treat as conflict if different, maybe we want to add?)
    # Current implementation: if current exists and new not in list -> Conflict
    params2 = {"party_roles": [{"role_code": "APPLICANT", "raw_text": "New Applicant"}]}
    result2 = resolver.detect_conflicts(params2)
    assert len(result2.conflicts) == 1
    assert result2.conflicts[0].new_value == "New Applicant"


def test_application_people_fields_use_conflict_flow_when_values_differ(sample_matter, db_session):
    ns = "domestic_patent"
    db_session.add(
        MatterCustomField(
            matter_id=str(sample_matter.matter_id),
            namespace=ns,
            data={
                "application_applicant_name": "Existing Applicant",
                "application_applicant_customer_no": "1-1111-111111-1",
            },
        )
    )
    db_session.commit()

    resolver = ParameterConflictResolver(sample_matter.matter_id)
    result = resolver.detect_conflicts(
        {
            "application_applicant_name": "Updated Applicant",
            "application_applicant_customer_no": "2-2222-222222-2",
        }
    )

    conflict_names = {item.field_name for item in result.conflicts}
    auto_apply_names = {item.field_name for item in result.auto_apply}

    assert "application_applicant_name" in conflict_names
    assert "application_applicant_customer_no" in conflict_names
    assert "application_applicant_name" not in auto_apply_names
    assert "application_applicant_customer_no" not in auto_apply_names


def test_pct_application_fields_use_custom_field_conflict_flow(sample_matter, db_session):
    ns = "domestic_patent"
    db_session.add(
        MatterCustomField(
            matter_id=str(sample_matter.matter_id),
            namespace=ns,
            data={
                "pct_application_no": "PCT/US2025/000001",
                "pct_application_date": "2025-01-02",
            },
        )
    )
    db_session.commit()

    resolver = ParameterConflictResolver(sample_matter.matter_id)
    result = resolver.detect_conflicts(
        {
            "pct_application_no": "PCT/US2024/050989",
            "pct_application_date": "2024-10-11",
        }
    )

    conflict_names = {item.field_name for item in result.conflicts}

    assert "pct_application_no" in conflict_names
    assert "pct_application_date" in conflict_names


def test_incoming_pct_priority_claims_do_not_create_foreign_filing_deadline(db_session):
    import uuid

    matter = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref="26PI0114",
        right_group="INC",
        matter_type="PATENT",
    )
    db_session.add(matter)
    db_session.commit()

    resolver = ParameterConflictResolver(matter.matter_id)
    result = resolver.detect_conflicts(
        {
            "doc_type": "Text Text203Text Text Text",
            "pct_application_no": "PCT/US2024/050989",
            "pct_application_date": "2024-10-11",
            "events": [{"event_key": "APP_DATE", "event_at": "2026-05-11"}],
            "priority_claims": [
                {"country": "US", "number": "63/544,133", "date": "2023-10-13"},
                {"country": "GB", "number": "2414953.6", "date": "2024-10-10"},
            ],
        }
    )

    auto_names = {item.field_name for item in result.auto_apply}

    assert "custom_priority_no" in auto_names
    assert "custom_priority_date" in auto_names
    assert "event_first_priority_date" in auto_names
    assert "foreign_filing_deadline" not in auto_names
    assert "custom_foreign_filing_deadline" not in auto_names


def test_inventor_party_roles_are_additive_auto_apply(sample_matter, db_session):
    import uuid

    from app.utils.policy_sql import policy_text as text

    db_session.execute(
        text(
            """
            INSERT INTO matter_party_role (mpr_id, matter_id, role_code, raw_text, seq)
            VALUES (:id, :mid, 'INVENTOR', 'Existing Inventor', 1)
            """
        ),
        {"id": uuid.uuid4().hex, "mid": sample_matter.matter_id},
    )
    db_session.commit()

    resolver = ParameterConflictResolver(sample_matter.matter_id)
    result = resolver.detect_conflicts(
        {
            "party_roles": [
                {"role_code": "INVENTOR", "raw_text": "Existing Inventor"},
                {"role_code": "INVENTOR", "raw_text": "Additional Inventor"},
            ]
        }
    )

    auto_apply_names = {item.field_name for item in result.auto_apply}
    conflict_names = {item.field_name for item in result.conflicts}

    assert "party_inventor_2" in auto_apply_names
    assert "party_inventor_2" not in conflict_names
    assert "inventor_name" not in auto_apply_names
    assert "inventor_name" not in conflict_names


def test_exam_request_date_conflict_is_not_auto_applied_when_hidden(sample_matter, db_session):
    ns = "domestic_patent"
    db_session.add(
        MatterCustomField(
            matter_id=str(sample_matter.matter_id),
            namespace=ns,
            data={"exam_request_date": "2020-01-01"},
        )
    )
    db_session.commit()

    resolver = ParameterConflictResolver(sample_matter.matter_id)
    result = resolver.detect_conflicts(
        {
            "events": [
                {
                    "event_key": "EXAM_REQ",
                    "raw_text": "Text",
                    "event_at": "2024-05-06",
                }
            ]
        }
    )

    conflict_names = {item.field_name for item in result.conflicts}
    auto_apply_names = {item.field_name for item in result.auto_apply}

    assert "exam_request_date" not in auto_apply_names
    assert "exam_request_date" not in conflict_names
