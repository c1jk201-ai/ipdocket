import uuid


def test_quick_search_invalid_limit_does_not_500(authenticated_client, monkeypatch):
    from app.blueprints.productivity import routes as prod_routes

    monkeypatch.setattr(prod_routes, "quick_search", lambda **kwargs: [])

    res = authenticated_client.get("/api/productivity/quick-searchNewq=test&limit=not-an-int")
    assert res.status_code == 200
    assert res.get_json()["ok"] is True


def test_quick_search_client_matches_case_and_separator_insensitive(
    authenticated_client, db_session
):
    from app.models.client import Client

    client = Client(
        name="ALPHA-CLIENT",
        registration_number="123-45-67890",
        email="alpha@example.com",
        is_deleted=False,
    )
    db_session.add(client)
    db_session.commit()

    res = authenticated_client.get("/api/productivity/quick-searchNewq=alphaclient&type=client")
    assert res.status_code == 200
    data = res.get_json() or {}
    items = data.get("items") or []
    assert any(str(item.get("title") or "") == "ALPHA-CLIENT" for item in items)

    res_digits = authenticated_client.get("/api/productivity/quick-searchNewq=1234567890&type=client")
    assert res_digits.status_code == 200
    data_digits = res_digits.get_json() or {}
    items_digits = data_digits.get("items") or []
    assert any(str(item.get("title") or "") == "ALPHA-CLIENT" for item in items_digits)


def test_quick_search_matter_supports_field_query_and_negation(admin_client, db_session):
    from app.models.case_flat_index import CaseFlatIndex
    from app.models.matter import Matter, VMatterOverview

    token = uuid.uuid4().hex[:8].upper()
    mid_keep = uuid.uuid4().hex
    mid_drop = uuid.uuid4().hex
    ref_keep = f"QS-MATTER-KEEP-{token}"
    ref_drop = f"QS-MATTER-DROP-{token}"
    client_name = f"ALPHA CLIENT {token}"

    db_session.add_all(
        [
            Matter(
                matter_id=mid_keep,
                our_ref=ref_keep,
                right_group="DOM",
                matter_type="PATENT",
                right_name=f"Allowed Matter {token}",
                is_deleted=False,
            ),
            Matter(
                matter_id=mid_drop,
                our_ref=ref_drop,
                right_group="DOM",
                matter_type="PATENT",
                right_name=f"Blocked Matter {token}",
                is_deleted=False,
            ),
            CaseFlatIndex(
                matter_id=mid_keep,
                client_name=client_name,
                search_text=f"{ref_keep} Allowed Matter {token} {client_name}",
            ),
            CaseFlatIndex(
                matter_id=mid_drop,
                client_name=client_name,
                search_text=f"{ref_drop} Blocked Matter {token} {client_name}",
            ),
            VMatterOverview(
                matter_id=mid_keep,
                our_ref=ref_keep,
                right_group="DOM",
                matter_type="PATENT",
                right_name=f"Allowed Matter {token}",
                clients=client_name,
            ),
            VMatterOverview(
                matter_id=mid_drop,
                our_ref=ref_drop,
                right_group="DOM",
                matter_type="PATENT",
                right_name=f"Blocked Matter {token}",
                clients=client_name,
            ),
        ]
    )
    db_session.commit()

    res = admin_client.get(
        "/api/productivity/quick-search",
        query_string={
            "q": f'client:"{client_name}" -Blocked',
            "type": "matter",
        },
    )
    assert res.status_code == 200
    data = res.get_json() or {}
    items = data.get("items") or []

    titles = [str(item.get("title") or "") for item in items]
    assert ref_keep in titles
    assert ref_drop not in titles
