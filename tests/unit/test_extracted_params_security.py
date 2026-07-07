"""
Regression tests for SQLi/tampering protection in extracted_params.
Tests the _resolve_conflict_meta validation function directly.
"""

import pytest


def test_resolve_conflict_meta_rejects_tampered_field_key(app, db_session):
    """
    Test that _resolve_conflict_meta returns None for a field_key
    containing SQL injection characters, effectively blocking the attack.
    """
    from app.blueprints.case.views.extracted_params import _resolve_conflict_meta

    # Tampered key with SQL injection attempt
    result = _resolve_conflict_meta(
        field_name="identifier_test",  # Uses identifier_ prefix, so goes to dynamic path
        table_name="matter_identifier",
        field_key="id_value = 'pwned' --",  # Contains SQL injection
    )

    # Should return None because field_key fails _FIELD_KEY_RE regex check
    # (contains spaces, equals, quotes, dashes)
    assert result is None, f"Expected None but got {result}"


def test_resolve_conflict_meta_rejects_unknown_field_name(app, db_session):
    """
    Test that _resolve_conflict_meta returns None for an unknown field_name
    that is not in conflict_definitions and doesn't match known prefixes.
    """
    from app.blueprints.case.views.extracted_params import _resolve_conflict_meta

    # Unknown field that's not in conflict_definitions
    result = _resolve_conflict_meta(
        field_name="unknown_field",
        table_name="matter",
        field_key="test",
    )

    # Should return None because:
    # - "unknown_field" is not in conflict_definitions
    # - "unknown_field" doesn't start with identifier_, event_, party_, staff_, custom_
    assert result is None, f"Expected None but got {result}"


def test_resolve_conflict_meta_accepts_valid_known_field(app, db_session):
    """
    Test that _resolve_conflict_meta properly resolves a valid known field.
    """
    from app.blueprints.case.views.extracted_params import _resolve_conflict_meta

    # Known field from conflict_definitions
    result = _resolve_conflict_meta(
        field_name="right_name",
        table_name="matter",  # This is ignored for known fields
        field_key="right_name",  # This is ignored for known fields
    )

    # Should return valid tuple (table, key, label, priority, hidden)
    assert result is not None, "Expected valid result for known field"
    table, key, label, priority, hidden = result
    assert table == "matter"
    assert key == "right_name"


def test_resolve_conflict_meta_accepts_valid_dynamic_field(app, db_session):
    """
    Test that _resolve_conflict_meta accepts valid dynamic field with safe key.
    """
    from app.blueprints.case.views.extracted_params import _resolve_conflict_meta

    # Valid dynamic field with identifier_ prefix
    result = _resolve_conflict_meta(
        field_name="identifier_APP_NO",
        table_name="matter_identifier",
        field_key="APP_NO",  # Safe key - alphanumeric with underscore
    )

    # Should return valid tuple
    assert result is not None, "Expected valid result for dynamic identifier field"
    table, key, label, priority, hidden = result
    assert table == "matter_identifier"
    assert key == "APP_NO"
