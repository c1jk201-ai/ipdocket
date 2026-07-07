from __future__ import annotations


def test_normalize_manual_workflow_category_maps_hybrid_aliases():
    from app.blueprints.workflow import routes as workflow_routes

    assert workflow_routes._normalize_manual_workflow_category("HYBRID") == "MGMT_WORK"
    assert workflow_routes._normalize_manual_workflow_category("mgmt_work") == "MGMT_WORK"
    assert workflow_routes._normalize_manual_workflow_category("work") == "WORK"


def test_derive_workflow_category_prefers_manual_category_over_assignment_mix(monkeypatch):
    from app.blueprints.workflow import routes as workflow_routes
    from app.utils import task_classification

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("manual category should bypass derived classification")

    monkeypatch.setattr(task_classification, "determine_category_by_staff_role", _unexpected)

    category = workflow_routes._derive_workflow_category(
        case_id="M-WF-CAT-1",
        handler_id=101,
        attorney_id=202,
        manager_id=303,
        manual_category="MGMT",
        hint_category=None,
        hint_name_ref="WF:GENERAL",
        hint_name_free="Text Text",
    )

    assert category == "MGMT"


def test_derive_workflow_category_manager_only_notice_wins_over_mixed(monkeypatch):
    from app.blueprints.workflow import routes as workflow_routes
    from app.utils import task_assignment_rules, task_classification

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("manager-only notice should resolve before staff-role fallback")

    monkeypatch.setattr(task_assignment_rules, "is_manager_only_notice", lambda **_kwargs: True)
    monkeypatch.setattr(task_classification, "determine_category_by_staff_role", _unexpected)

    category = workflow_routes._derive_workflow_category(
        case_id="M-WF-CAT-2",
        handler_id=101,
        attorney_id=None,
        manager_id=303,
        manual_category=None,
        hint_category=None,
        hint_name_ref="WF:NOTICE",
        hint_name_free="Text Text",
        source="uspto_notice",
    )

    assert category == "MGMT"


def test_case_view_workflow_category_respects_explicit_manual_value():
    from app.blueprints.case.services import detail_context

    assert (
        detail_context._resolve_case_view_workflow_category(
            "MGMT",
            has_manager=True,
            has_work_assignee=True,
        )
        == "mgmt"
    )
    assert (
        detail_context._resolve_case_view_workflow_category(
            "MGMT_WORK",
            has_manager=False,
            has_work_assignee=False,
        )
        == "hybrid"
    )


def test_workflow_primary_owner_user_id_prefers_manager_for_explicit_mgmt():
    from app.utils.workflow_semantics import workflow_primary_owner_user_id

    assert (
        workflow_primary_owner_user_id(
            category="MGMT",
            handler_id=101,
            attorney_id=202,
            manager_id=303,
        )
        == 303
    )


def test_workflow_owner_role_codes_follow_category_rules():
    from app.utils.workflow_semantics import workflow_owner_role_codes

    assert workflow_owner_role_codes(
        category="WORK", handler_id=101, attorney_id=202, manager_id=303
    ) == (
        "attorney",
        "handler",
    )
    assert workflow_owner_role_codes(
        category="MGMT", handler_id=101, attorney_id=202, manager_id=303
    ) == ("manager",)
    assert workflow_owner_role_codes(
        category="MGMT_WORK", handler_id=101, attorney_id=202, manager_id=303
    ) == (
        "attorney",
        "handler",
        "manager",
    )


def test_workflow_sync_category_types_keep_explicit_work_single_stream():
    from app.utils.workflow_semantics import workflow_sync_category_types

    assert workflow_sync_category_types("WORK", has_hybrid_assignments=True) == ("WORK",)
    assert workflow_sync_category_types("HYBRID", has_hybrid_assignments=False) == (
        "WORK",
        "MGMT",
    )


def test_requested_manual_workflow_category_ignores_auto_recommended_value():
    from app.blueprints.workflow import routes as workflow_routes

    assert (
        workflow_routes._requested_manual_workflow_category(
            {"category": "MGMT_WORK", "category_manual": "0"}
        )
        is None
    )
    assert (
        workflow_routes._requested_manual_workflow_category(
            {"category": "MGMT", "category_manual": "1"}
        )
        == "MGMT"
    )
