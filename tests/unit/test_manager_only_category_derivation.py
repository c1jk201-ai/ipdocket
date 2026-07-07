from __future__ import annotations


def test_derive_notice_workflow_category_manager_only_wins_over_mixed(monkeypatch):
    from app.blueprints.api import routes as api_routes

    monkeypatch.setattr(api_routes, "is_manager_only_notice", lambda **_kwargs: True)

    category = api_routes._derive_notice_workflow_category(
        matter_id="M-CAT-1",
        doc_title="Text Text(3Text Text) · Text",
        assignee_id=11,
        attorney_assignee_id=22,
        manager_assignee_id=33,
        source="uspto_notice",
    )
    assert category == "MGMT"


def test_derive_notice_workflow_category_keeps_mixed_when_not_manager_only(monkeypatch):
    from app.blueprints.api import routes as api_routes

    monkeypatch.setattr(api_routes, "is_manager_only_notice", lambda **_kwargs: False)

    category = api_routes._derive_notice_workflow_category(
        matter_id="M-CAT-2",
        doc_title="Text Text",
        assignee_id=11,
        attorney_assignee_id=22,
        manager_assignee_id=33,
        source="uspto_notice",
    )
    assert category == "MGMT_WORK"


def test_derive_quick_workflow_category_manager_only_wins_over_mixed(monkeypatch):
    from app.blueprints.case import quick_routes

    monkeypatch.setattr(quick_routes, "is_manager_only_notice", lambda **_kwargs: True)

    category = quick_routes._derive_quick_workflow_category(
        matter_id="M-CAT-3",
        workflow_code="WF:TEST",
        title="Text Text(3Text Text) · Text",
        handler_uid=11,
        attorney_uid=22,
        manager_uid=33,
    )
    assert category == "MGMT"


def test_derive_quick_workflow_category_keeps_mixed_when_not_manager_only(monkeypatch):
    from app.blueprints.case import quick_routes

    monkeypatch.setattr(quick_routes, "is_manager_only_notice", lambda **_kwargs: False)

    category = quick_routes._derive_quick_workflow_category(
        matter_id="M-CAT-4",
        workflow_code="WF:GENERAL",
        title="Text Text",
        handler_uid=11,
        attorney_uid=22,
        manager_uid=33,
    )
    assert category == "MGMT_WORK"
