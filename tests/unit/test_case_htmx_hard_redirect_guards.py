from pathlib import Path


def test_hx_redirect_helper_enforces_htmx_header_and_redirect() -> None:
    source = Path("app/blueprints/case/helpers.py").read_text(encoding="utf-8")

    assert 'request.headers.get("HX-Request")' in source
    assert '"HX-Redirect"' in source


def test_edit_matter_uses_hx_redirect_helper() -> None:
    source = Path("app/blueprints/case/routes/general_edit.py").read_text(encoding="utf-8")

    assert "_hx_hard_redirect_response(" in source
    assert '"case_work.edit_matter"' in source


def test_create_routes_have_hx_redirect_guard() -> None:
    source = Path("app/blueprints/case/routes/general_create.py").read_text(encoding="utf-8")

    assert (
        '_hx_hard_redirect_response("case_work.intake_matter", **request.args.to_dict())' in source
    )
    assert (
        '_hx_hard_redirect_response("case_work.create_matter", **request.args.to_dict())' in source
    )
