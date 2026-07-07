def test_views_list_includes_team_views_for_same_department(app, db_session, client, sample_user):
    from app.models.user import User
    from app.models.user_saved_view import UserSavedView

    sample_user.department = "teamA"
    user2 = User(
        username="user2",
        email="user2@example.com",
        role="patent_staff",
        is_active=True,
        department="teamA",
    )
    user3 = User(
        username="user3",
        email="user3@example.com",
        role="patent_staff",
        is_active=True,
        department="teamB",
    )
    db_session.add_all([sample_user, user2, user3])
    db_session.commit()

    team_view = UserSavedView(
        user_id=sample_user.id,
        scope="team",
        scope_key="teamA",
        module="case_list",
        name="Team View",
        payload_json={"path": "/case/list"},
    )
    private_view_user2 = UserSavedView(
        user_id=user2.id,
        scope="private",
        module="case_list",
        name="User2 Private",
        payload_json={"path": "/case/list"},
    )
    db_session.add_all([team_view, private_view_user2])
    db_session.commit()

    # user2 (same dept) should see: user2 private + team view
    with client.session_transaction() as session:
        session["_user_id"] = user2.id
        session["_fresh"] = True

    resp = client.get("/api/views?module=case_list")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    items = data.get("items") or []
    assert any(v.get("name") == "Team View" and v.get("scope") == "team" for v in items)
    assert any(v.get("name") == "User2 Private" and v.get("scope") != "team" for v in items)

    # user3 (different dept) should NOT see team view
    with client.session_transaction() as session:
        session["_user_id"] = user3.id
        session["_fresh"] = True

    resp = client.get("/api/views?module=case_list")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    items = data.get("items") or []
    assert not any(v.get("name") == "Team View" for v in items)


def test_views_create_team_requires_department(app, db_session, authenticated_client, sample_user):
    sample_user.department = ""
    db_session.add(sample_user)
    db_session.commit()

    resp = authenticated_client.post(
        "/api/views",
        json={
            "module": "case_list",
            "name": "Team View",
            "scope": "team",
            "payload": {"path": "/case/list"},
        },
    )
    assert resp.status_code == 400


def test_views_set_default_rejects_team_view(app, db_session, client, sample_user):
    from app.models.user_saved_view import UserSavedView

    sample_user.department = "teamA"
    db_session.add(sample_user)
    db_session.commit()

    team_view = UserSavedView(
        user_id=sample_user.id,
        scope="team",
        scope_key="teamA",
        module="case_list",
        name="Team View",
        payload_json={"path": "/case/list"},
    )
    db_session.add(team_view)
    db_session.commit()

    with client.session_transaction() as session:
        session["_user_id"] = sample_user.id
        session["_fresh"] = True

    resp = client.post(f"/api/views/{team_view.id}/set-default")
    assert resp.status_code == 400
