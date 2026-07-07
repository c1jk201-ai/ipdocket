import pytest


@pytest.fixture
def mgmt_director_user(app, db_session):
    from app.models.user import User

    user = User.query.filter_by(email="mgmt_director@example.com").first()
    if not user:
        user = User(
            username="mgmt_director",
            email="mgmt_director@example.com",
            role="mgmt_director",
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
    db_session.refresh(user)
    user._test_id = user.id
    return user


@pytest.fixture
def partner_attorney_user(app, db_session):
    from app.models.user import User

    user = User.query.filter_by(email="partner_attorney@example.com").first()
    if not user:
        user = User(
            username="partner_attorney",
            email="partner_attorney@example.com",
            role="partner_attorney",
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
    db_session.refresh(user)
    user._test_id = user.id
    return user


@pytest.fixture
def mgmt_director_client(client, mgmt_director_user):
    user_id = getattr(mgmt_director_user, "_test_id", None) or mgmt_director_user.id
    with client.session_transaction() as session:
        session["_user_id"] = user_id
        session["_fresh"] = True
    return client


@pytest.fixture
def partner_attorney_client(client, partner_attorney_user):
    user_id = getattr(partner_attorney_user, "_test_id", None) or partner_attorney_user.id
    with client.session_transaction() as session:
        session["_user_id"] = user_id
        session["_fresh"] = True
    return client


@pytest.mark.parametrize(
    "client_fixture",
    ["mgmt_director_client", "partner_attorney_client"],
)
def test_super_roles_can_list_cases_without_assignment(request, client_fixture, sample_matter):
    client = request.getfixturevalue(client_fixture)
    matter_id = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)

    res = client.get("/api/cases")
    assert res.status_code == 200
    data = res.get_json()
    assert isinstance(data, list)
    assert any(str(item.get("matter_id")) == matter_id for item in data)


@pytest.mark.parametrize(
    "client_fixture",
    ["mgmt_director_client", "partner_attorney_client"],
)
def test_super_roles_can_view_case_detail_without_assignment(
    request, client_fixture, sample_matter
):
    client = request.getfixturevalue(client_fixture)
    matter_id = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)

    res = client.get(f"/case/{matter_id}")
    assert res.status_code in (200, 302)


@pytest.mark.parametrize("role", ["mgmt_director", "partner_attorney"])
def test_super_roles_can_delete_cases_via_permissions(app, db_session, sample_matter, role):
    from app.models.user import User
    from app.utils.permissions import can_access_matter

    user = User(username=f"tmp_{role}", email=f"tmp_{role}@example.com", role=role, is_active=True)
    db_session.add(user)
    db_session.commit()

    assert can_access_matter(user, str(sample_matter.matter_id), action="delete_case") is True
    assert can_access_matter(user, str(sample_matter.matter_id), action="invoice") is True


@pytest.mark.parametrize(
    "client_fixture",
    ["mgmt_director_client", "partner_attorney_client"],
)
def test_super_roles_still_cannot_access_admin_system_endpoints(request, client_fixture):
    client = request.getfixturevalue(client_fixture)

    res = client.get("/admin/api/users")
    assert res.status_code in (401, 403, 302)


@pytest.mark.parametrize(
    "client_fixture",
    ["mgmt_director_client", "partner_attorney_client"],
)
def test_super_roles_can_create_and_delete_deadlines(request, client_fixture, sample_matter):
    from app.models.docket import DocketItem

    client = request.getfixturevalue(client_fixture)
    matter_id = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)

    create_res = client.post(
        "/deadline/api/deadlines",
        json={
            "matter_id": matter_id,
            "title": "Text Text",
            "internal_due_date": "2026-01-01",
        },
    )
    assert create_res.status_code == 201
    created = create_res.get_json()
    assert isinstance(created, dict)
    assert created.get("matter_id") == matter_id
    docket_id = created.get("id")
    assert docket_id
    created_row = DocketItem.query.get(docket_id)
    assert created_row is not None
    assert (created_row.due_date or "") == ""
    assert created_row.extended_due_date == "2026-01-01"

    delete_res = client.delete(f"/deadline/api/deadlines/{docket_id}")
    assert delete_res.status_code == 200
    payload = delete_res.get_json()
    assert payload.get("success") is True


def test_admin_can_access_crm_client_merge_page(admin_client):
    res = admin_client.get("/crm/clients/merge")
    assert res.status_code == 200


@pytest.mark.parametrize(
    "client_fixture",
    ["mgmt_director_client", "partner_attorney_client"],
)
def test_non_admin_cannot_access_crm_client_merge_page(request, client_fixture):
    client = request.getfixturevalue(client_fixture)
    res = client.get("/crm/clients/merge")
    assert res.status_code == 403


def test_admin_can_post_invoice_client_merge(admin_client):
    res = admin_client.post("/accounting/invoice-system/clients/merge", data={})
    assert res.status_code == 302


def test_non_admin_cannot_post_invoice_client_merge(mgmt_director_client):
    res = mgmt_director_client.post("/accounting/invoice-system/clients/merge", data={})
    assert res.status_code == 403


def test_invoice_client_undo_merge_is_admin_only(mgmt_director_client):
    res = mgmt_director_client.post("/accounting/invoice-system/clients/undo_merge/1", data={})
    assert res.status_code == 403
