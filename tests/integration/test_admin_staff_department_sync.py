from __future__ import annotations

import uuid


def test_admin_staff_create_syncs_department_to_matching_user(admin_client, db_session):
    from app.models.user import User

    username = f"staffsync_{uuid.uuid4().hex[:8]}"
    user = User(username=username, role="user", is_active=True)
    db_session.add(user)
    db_session.commit()

    resp = admin_client.post(
        "/admin/api/staff",
        json={
            "staff_code": username,
            "name_display": "Text Text",
            "dept": "Text",
            "email": f"{username}@example.com",
            "active": True,
        },
    )
    assert resp.status_code == 200

    saved_user = User.query.filter_by(username=username).first()
    assert saved_user is not None
    assert saved_user.staff_party_id
    assert saved_user.department == "Text"


def test_admin_staff_update_syncs_department_to_linked_user(admin_client, db_session):
    from app.models.party import Party, PartyStaff
    from app.models.user import User

    username = f"staffsync_{uuid.uuid4().hex[:8]}"
    party_id = uuid.uuid4().hex

    user = User(username=username, role="user", is_active=True)
    party = Party(
        party_id=party_id,
        name_display="Text Text Text",
        name_en="Text Text Text",
        created_at="2026-04-01 00:00:00",
    )
    staff = PartyStaff(party_id=party_id, staff_code=username, dept=None, active=1)
    db_session.add_all([user, party, staff])
    db_session.commit()

    resp = admin_client.patch(
        f"/admin/api/staff/{party_id}",
        json={
            "name_display": "Text Text Text",
            "dept": "Text",
            "email": f"{username}@example.com",
            "active": True,
        },
    )
    assert resp.status_code == 200

    saved_user = User.query.filter_by(username=username).first()
    saved_staff = PartyStaff.query.filter_by(party_id=party_id).first()
    assert saved_staff is not None
    assert saved_user is not None
    assert saved_staff.dept == "Text"
    assert saved_user.staff_party_id == party_id
    assert saved_user.department == "Text"
