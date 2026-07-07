from __future__ import annotations

import re
from pathlib import Path

import pytest

CSRF_HIDDEN_FIELD_PATTERN = (
    r'<input type="hidden" name="csrf_token" value="\{\{ csrf_token\(\) \}\}"'
)
HISTORY_TEMPLATE_PATHS = (
    "app/templates/case/matter_view/_sections_after_files.html",
    "app/templates/case/matter_view/partials/_sec_history_content.html",
)
MEMO_TEMPLATE_PATH = "app/templates/case/matter_view/partials/_sec_memo_content.html"


def _read_existing_template_sources(*relative_paths: str) -> str:
    sources: list[str] = []
    for rel in relative_paths:
        path = Path(rel)
        if path.exists():
            sources.append(path.read_text(encoding="utf-8"))
    return "\n".join(sources)


def _assert_form_action_has_csrf(source: str, action_pattern: str) -> None:
    pattern = rf"{action_pattern}[\s\S]{{0,220}}{CSRF_HIDDEN_FIELD_PATTERN}"
    assert re.search(pattern, source)


def test_history_notice_delete_passes_required_csrf_args(app, monkeypatch):
    from app.blueprints.case.views import history_notice as history_notice_view

    case_id = "case-id"
    oa_id = "oa-id"
    expected_response = object()
    captured: dict = {}

    def _fake_validate_csrf(form_token, redirect_endpoint, **redirect_kwargs):
        captured["form_token"] = form_token
        captured["redirect_endpoint"] = redirect_endpoint
        captured["redirect_kwargs"] = redirect_kwargs
        return expected_response

    monkeypatch.setattr(history_notice_view, "validate_csrf_or_redirect", _fake_validate_csrf)

    with app.test_request_context(
        f"/case/{case_id}/history/notice/{oa_id}/delete",
        method="POST",
        data={"csrf_token": "csrf-test-token"},
    ):
        response = history_notice_view.history_notice_delete.__wrapped__(case_id, oa_id)

    assert response is expected_response
    assert captured == {
        "form_token": "csrf-test-token",
        "redirect_endpoint": "case_work.case_detail",
        "redirect_kwargs": {"case_id": case_id},
    }


@pytest.mark.parametrize(
    "action_pattern",
    [
        r"url_for\('case_work\.history_notice_delete', case_id=matter\.matter_id, oa_id=r\.id\)",
    ],
)
def test_notice_delete_form_includes_csrf_hidden_field(action_pattern):
    source = _read_existing_template_sources(*HISTORY_TEMPLATE_PATHS)
    _assert_form_action_has_csrf(source, action_pattern)


@pytest.mark.parametrize(
    "action_pattern",
    [
        r"url_for\('case_work\.memo_add', case_id=matter\.matter_id\)",
        r"url_for\('case_work\.memo_attachment_delete', case_id=matter\.matter_id, memo_id=m\.id, memo_file_id=a\.memo_file_id\)",
        r"url_for\('case_work\.memo_delete', case_id=matter\.matter_id, memo_id=m\.id\)",
    ],
)
def test_memo_forms_include_csrf_hidden_fields(action_pattern):
    source = _read_existing_template_sources(MEMO_TEMPLATE_PATH)
    _assert_form_action_has_csrf(source, action_pattern)
