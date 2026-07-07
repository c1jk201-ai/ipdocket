def test_views_list_includes_role_aware_system_views(app, db_session, client, sample_user):
    sample_user.role = "patent_staff"
    sample_user.staff_party_id = "staff-1"
    db_session.add(sample_user)
    db_session.commit()

    with client.session_transaction() as session:
        session["_user_id"] = sample_user.id
        session["_fresh"] = True

    resp = client.get("/api/views?module=case_list")
    assert resp.status_code == 200
    items = (resp.get_json() or {}).get("items") or []

    my_cases = next(v for v in items if v.get("id") == "system:case_list:my_cases")
    assert my_cases["scope"] == "system"
    assert my_cases["is_default"] is True
    assert "/case/list" in my_cases["url"]
    assert "assigned=me" in my_cases["url"]


def test_personal_default_suppresses_system_default(app, db_session, client, sample_user):
    from app.models.user_saved_view import UserSavedView

    sample_user.role = "patent_staff"
    sample_user.staff_party_id = "staff-1"
    db_session.add(sample_user)
    db_session.flush()
    db_session.add(
        UserSavedView(
            user_id=sample_user.id,
            scope="private",
            module="case_list",
            name="My Default",
            payload_json={"path": "/case/list", "filters": {"q": "ABC"}},
            is_default=True,
        )
    )
    db_session.commit()

    with client.session_transaction() as session:
        session["_user_id"] = sample_user.id
        session["_fresh"] = True

    resp = client.get("/api/views?module=case_list")
    assert resp.status_code == 200
    items = (resp.get_json() or {}).get("items") or []

    personal = next(v for v in items if v.get("name") == "My Default")
    system = next(v for v in items if v.get("id") == "system:case_list:my_cases")
    assert personal["is_default"] is True
    assert system["is_default"] is False


def test_invoice_and_customer_system_views_have_expected_filters(
    app, db_session, client, sample_user
):
    sample_user.role = "accounting"
    db_session.add(sample_user)
    db_session.commit()

    with client.session_transaction() as session:
        session["_user_id"] = sample_user.id
        session["_fresh"] = True

    invoice_resp = client.get("/api/views?module=invoice_list")
    client_resp = client.get("/api/viewsNewmodule=invoice_client_list")
    crm_resp = client.get("/api/views?module=crm_client_list")
    assert invoice_resp.status_code == 200
    assert client_resp.status_code == 200
    assert crm_resp.status_code == 200

    invoice_items = (invoice_resp.get_json() or {}).get("items") or []
    client_items = (client_resp.get_json() or {}).get("items") or []
    crm_items = (crm_resp.get_json() or {}).get("items") or []

    outstanding = next(v for v in invoice_items if v.get("id") == "system:invoice_list:outstanding")
    outstanding_clients = next(
        v for v in client_items if v.get("id") == "system:invoice_client_list:outstanding"
    )
    missing_invoice = next(
        v for v in crm_items if v.get("id") == "system:crm_client_list:invoice_missing"
    )

    assert "status=sent_unpaid_or_pending" in outstanding["url"]
    assert outstanding["is_default"] is True
    assert "has_outstanding=1" in outstanding_clients["url"]
    assert "invoice_link=missing" in missing_invoice["url"]


