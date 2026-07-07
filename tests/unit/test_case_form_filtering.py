def test_custom_field_filtering_ignores_routing_and_profile_context_keys(app, monkeypatch):
    from app.blueprints.case.helpers import _log_custom_field_filtering
    from app.services.case.form_support import log_custom_field_filtering

    warnings = []
    form_data = {
        "category": "TRADEMARK",
        "in_out_type": "DOM",
        "application_country": "US",
    }

    with app.app_context():
        monkeypatch.setattr(app.logger, "warning", lambda *args, **kwargs: warnings.append(args))
        log_custom_field_filtering(
            matter_id="matter-1",
            namespace="domestic_trademark",
            form_data=form_data,
            allowed_keys=[],
        )
        _log_custom_field_filtering(
            matter_id="matter-1",
            namespace="domestic_trademark",
            form_data=form_data,
            allowed_keys=[],
        )

    assert warnings == []


def test_custom_field_filtering_still_reports_unknown_keys(app, monkeypatch):
    from app.services.case.form_support import log_custom_field_filtering

    warnings = []

    with app.app_context():
        monkeypatch.setattr(app.logger, "warning", lambda *args, **kwargs: warnings.append(args))
        log_custom_field_filtering(
            matter_id="matter-1",
            namespace="domestic_trademark",
            form_data={"definitely_unknown_form_key": "x"},
            allowed_keys=[],
        )

    assert warnings
    assert "Case update key filtering" in warnings[0][0]
    assert "definitely_unknown_form_key" in warnings[0]
