def test_build_view_url_rejects_absolute_url(app):
    from app.models.user_saved_view import UserSavedView
    from app.services.productivity.view_service import build_view_url

    v = UserSavedView(
        id="view1",
        user_id=1,
        scope="private",
        module="case_list",
        name="test",
        payload_json={"path": "https://evil.example/phish?x=1"},
    )
    assert build_view_url(v) == "/case/list?view_id=view1"


def test_build_view_url_rejects_scheme_relative_url(app):
    from app.models.user_saved_view import UserSavedView
    from app.services.productivity.view_service import build_view_url

    v = UserSavedView(
        id="view2",
        user_id=1,
        scope="private",
        module="worklog",
        name="test",
        payload_json={"path": "//evil.example/phish"},
    )
    assert build_view_url(v) == "/worklog?view_id=view2"


def test_build_view_url_rejects_non_slash_path(app):
    from app.models.user_saved_view import UserSavedView
    from app.services.productivity.view_service import build_view_url

    v = UserSavedView(
        id="view3",
        user_id=1,
        scope="private",
        module="case_list",
        name="test",
        payload_json={"path": "case/list"},
    )
    assert build_view_url(v) == "/case/list?view_id=view3"


def test_build_view_url_allows_relative_path_with_query(app):
    from app.models.user_saved_view import UserSavedView
    from app.services.productivity.view_service import build_view_url

    v = UserSavedView(
        id="view4",
        user_id=1,
        scope="private",
        module="case_list",
        name="test",
        payload_json={"path": "/case/list?foo=bar"},
    )
    assert build_view_url(v) == "/case/list?foo=bar&view_id=view4"
