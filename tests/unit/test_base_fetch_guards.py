from pathlib import Path


def test_base_template_does_not_override_response_json_globally() -> None:
    source = Path("app/templates/layouts/base.html").read_text(encoding="utf-8")

    assert "Response.prototype.json =" not in source
    assert "wrapResponseJson" not in source


def test_base_template_detects_redirected_login_fetches() -> None:
    source = Path("app/templates/layouts/base.html").read_text(encoding="utf-8")

    assert "const isAuthPageUrl = (rawUrl) => {" in source
    assert "res.redirected && isAuthPageUrl(res.url)" in source
    assert "window.location.assign(res.url);" in source


def test_base_template_loads_flatpickr_before_app_core() -> None:
    source = Path("app/templates/layouts/base.html").read_text(encoding="utf-8")

    assert "vendor/flatpickr/flatpickr.min.css" in source
    assert source.index("vendor/flatpickr/flatpickr.min.js") < source.index("js/app_core.js")


def test_app_core_observes_dynamic_strict_date_inputs() -> None:
    source = Path("app/static/js/app_core.js").read_text(encoding="utf-8")

    assert "function initStrictDateMutationObserver()" in source
    assert "MutationObserver" in source
    assert 'attributeFilter: ["type", "data-ipm-date-input"]' in source
    assert "if (isStrictDateControl(scope)) setupStrictDateInput(scope);" in source
    assert "initStrictDateMutationObserver();" in source


def test_case_quick_panel_dates_use_global_datepicker_contract() -> None:
    source = Path("app/templates/case/_case_quick_panel.html").read_text(
        encoding="utf-8"
    )

    for field_name in ("legal_due_date", "due_date", "visible_from_date"):
        assert f'name="{field_name}"' in source
        before, _match, after = source.partition(f'name="{field_name}"')
        input_start = before.rfind("<input")
        input_tag = before[input_start:] + f'name="{field_name}"' + after.split(">", 1)[0]
        assert 'type="date"' in input_tag
        assert 'data-ipm-date-input="1"' in input_tag
