from __future__ import annotations

import uuid


def _add_matter(
    db_session,
    *,
    our_ref: str,
    right_name: str,
    right_group: str = "OUT",
    matter_type: str = "PATENT",
):
    from app.models.ip_records import Matter

    m = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=our_ref,
        right_name=right_name,
        right_group=right_group,
        matter_type=matter_type,
    )
    db_session.add(m)
    return m


def test_family_candidate_api_rejects_cross_year_series_even_when_title_matches(
    app, db_session, admin_client
):
    _add_matter(
        db_session,
        our_ref="26PO0102US",
        right_name="Stackable battery module and cooling plate",
    )
    _add_matter(
        db_session,
        our_ref="25PO0102US",
        right_name="Stackable battery module",
    )
    _add_matter(
        db_session,
        our_ref="24PO0102US",
        right_name="Stackable battery module and cooling plate",
    )
    db_session.commit()

    res = admin_client.get(
        "/case/api/check_family_candidate",
        query_string={
            "our_ref": "26PO0102US",
            "title": "Stackable battery module and cooling plate",
        },
    )
    assert res.status_code == 200
    payload = res.get_json() or {}
    assert payload.get("value") is None


def test_family_candidate_api_returns_same_year_base_candidate_with_compatible_title(
    app, db_session, admin_client
):
    _add_matter(
        db_session,
        our_ref="26PO0102US",
        right_name="Stackable battery module and cooling plate",
    )
    _add_matter(
        db_session,
        our_ref="26PO0102JP",
        right_name="Stackable battery module",
    )
    db_session.commit()

    res = admin_client.get(
        "/case/api/check_family_candidate",
        query_string={
            "our_ref": "26PO0102US",
            "title": "Stackable battery module and cooling plate",
        },
    )
    assert res.status_code == 200
    payload = res.get_json() or {}
    value = payload.get("value") or {}
    assert (value.get("our_ref") or "").strip() == "26PO0102JP"


def test_family_candidate_api_hides_inaccessible_candidate(app, db_session, authenticated_client):
    _add_matter(
        db_session,
        our_ref="26PO0102US",
        right_name="Stackable battery module and cooling plate",
    )
    _add_matter(
        db_session,
        our_ref="26PO0102JP",
        right_name="Stackable battery module",
    )
    db_session.commit()

    res = authenticated_client.get(
        "/case/api/check_family_candidate",
        query_string={
            "our_ref": "26PO0102US",
            "title": "Stackable battery module and cooling plate",
        },
    )
    assert res.status_code == 200
    payload = res.get_json() or {}
    assert payload.get("value") is None


def test_family_candidate_api_hides_soft_deleted_candidate(app, db_session, admin_client):
    _add_matter(
        db_session,
        our_ref="26PO0102US",
        right_name="Stackable battery module and cooling plate",
    )
    deleted = _add_matter(
        db_session,
        our_ref="26PO0102JP",
        right_name="Stackable battery module",
    )
    deleted.is_deleted = True
    db_session.commit()

    res = admin_client.get(
        "/case/api/check_family_candidate",
        query_string={
            "our_ref": "26PO0102US",
            "title": "Stackable battery module and cooling plate",
        },
    )
    assert res.status_code == 200
    payload = res.get_json() or {}
    assert payload.get("value") is None


def test_family_candidate_api_returns_none_for_mismatched_title_hint(app, db_session, admin_client):
    _add_matter(
        db_session,
        our_ref="26PO0102US",
        right_name="Stackable battery module and cooling plate",
    )
    _add_matter(
        db_session,
        our_ref="26PO0102JP",
        right_name="Optical lens inspection system",
    )
    db_session.commit()

    res = admin_client.get(
        "/case/api/check_family_candidate",
        query_string={
            "our_ref": "26PO0102US",
            "title": "Stackable battery module and cooling plate",
        },
    )
    assert res.status_code == 200
    payload = res.get_json() or {}
    assert payload.get("value") is None


def test_family_candidate_api_rejects_domestic_candidate_for_out_pct_ref(
    app, db_session, admin_client
):
    _add_matter(
        db_session,
        our_ref="26PD0103US",
        right_name="Stackable battery module and cooling plate",
        right_group="DOM",
        matter_type="PATENT",
    )
    db_session.commit()

    res = admin_client.get(
        "/case/api/check_family_candidate",
        query_string={
            "our_ref": "26PD0103PCT",
            "title": "Stackable battery module and cooling plate",
            "division": "OUT",
            "type": "PCT",
        },
    )
    assert res.status_code == 200
    payload = res.get_json() or {}
    assert payload.get("value") is None
