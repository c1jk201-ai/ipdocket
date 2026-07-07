def test_merge_keeps_handler_empty_when_other_roles_are_explicit(app, monkeypatch):
    from app.blueprints.workflow import routes

    monkeypatch.setattr(
        routes,
        "_resolve_case_role_user_ids",
        lambda _case_id: {"handler": 11, "attorney": 22, "manager": 33},
    )

    handler_id, attorney_id, manager_id = routes._merge_workflow_assignees_with_case_defaults(
        case_id="M-1",
        handler_id=None,
        attorney_id=2,
        manager_id=3,
        fallback_handler_id=99,
    )

    assert handler_id is None
    assert attorney_id == 2
    assert manager_id == 3


def test_merge_fills_defaults_when_all_roles_missing(app, monkeypatch):
    from app.blueprints.workflow import routes

    monkeypatch.setattr(
        routes,
        "_resolve_case_role_user_ids",
        lambda _case_id: {"handler": 11, "attorney": 22, "manager": 33},
    )

    handler_id, attorney_id, manager_id = routes._merge_workflow_assignees_with_case_defaults(
        case_id="M-2",
        handler_id=None,
        attorney_id=None,
        manager_id=None,
        fallback_handler_id=99,
    )

    assert handler_id == 11
    assert attorney_id == 22
    assert manager_id == 33


def test_merge_uses_handler_fallback_only_when_no_other_roles(app, monkeypatch):
    from app.blueprints.workflow import routes

    monkeypatch.setattr(
        routes,
        "_resolve_case_role_user_ids",
        lambda _case_id: {"handler": None, "attorney": None, "manager": None},
    )

    handler_id, attorney_id, manager_id = routes._merge_workflow_assignees_with_case_defaults(
        case_id="M-3",
        handler_id=None,
        attorney_id=None,
        manager_id=None,
        fallback_handler_id=99,
    )

    assert handler_id == 99
    assert attorney_id is None
    assert manager_id is None
