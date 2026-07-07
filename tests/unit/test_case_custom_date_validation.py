import pytest


def test_edit_custom_field_validator_rejects_six_digit_year(app, db_session):
    from app.blueprints.case.helpers import _validate_custom_field_updates

    with app.app_context():
        with pytest.raises(ValueError) as exc:
            _validate_custom_field_updates(
                matter_id="date-validation-test",
                namespace="domestic_design",
                form_data={"novelty_grace_date": "222222-02-02"},
                allowed_keys=["novelty_grace_date"],
                strict_dates=True,
            )

    assert "Invalid date format" in str(exc.value)
