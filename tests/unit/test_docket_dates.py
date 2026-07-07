def test_normalize_date_str_skips_nullish_tokens_without_parse_failure(app, monkeypatch):
    from app.utils.docket_dates import normalize_date_str

    captured = []

    with app.app_context():
        import app.services.automation.parse_failure as parse_failure_mod

        monkeypatch.setattr(
            parse_failure_mod,
            "record_parse_failure",
            lambda **kwargs: captured.append(kwargs),
        )
        for raw in ("null", "NULL", " none ", "[null]", "NaN", "<nil>"):
            assert normalize_date_str(raw) is None

    assert captured == []


def test_normalize_date_str_records_parse_failure_for_invalid_text(app, monkeypatch):
    from app.utils.docket_dates import normalize_date_str

    captured = []

    with app.app_context():
        import app.services.automation.parse_failure as parse_failure_mod

        monkeypatch.setattr(
            parse_failure_mod,
            "record_parse_failure",
            lambda **kwargs: captured.append(kwargs),
        )
        assert normalize_date_str("not-a-date") is None

    assert len(captured) == 1
    assert captured[0].get("kind") == "date"
    assert captured[0].get("error") == "no_match"
    assert captured[0].get("source") == "docket_dates.normalize_date_str"
    assert captured[0].get("raw_value") == "not-a-date"


def test_normalize_date_str_rejects_six_digit_year(app, monkeypatch):
    from app.utils.docket_dates import normalize_date_str

    captured = []

    with app.app_context():
        import app.services.automation.parse_failure as parse_failure_mod

        monkeypatch.setattr(
            parse_failure_mod,
            "record_parse_failure",
            lambda **kwargs: captured.append(kwargs),
        )
        assert normalize_date_str("222222-02-02") is None
        assert normalize_date_str("some text 2026-08-20 more text") == "2026-08-20"

    assert captured
    assert captured[0].get("raw_value") == "222222-02-02"
