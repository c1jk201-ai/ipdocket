from __future__ import annotations

import uuid

from app.models.matter import Matter, MatterStaffAssignment
from app.models.party import Party, PartyStaff
from app.models.user import User


def test_admin_access_debug_explains_visibility_and_updates_on_assignment(
    admin_client, db_session, monkeypatch
):
    from app.services.core.config_service import ConfigService

    monkeypatch.setenv("ADMIN_CIDR_ALLOWLIST", "127.0.0.1/32")
    ConfigService.clear_cache()
    req_headers = {
        "X-Forwarded-For": "127.0.0.1",
        "X-Forwarded-Proto": "https",
    }

    matter_id = uuid.uuid4().hex
    matter = Matter(
        matter_id=matter_id,
        our_ref=f"DBG-{uuid.uuid4().hex[:8]}",
        right_name="Access Debug Case",
        is_deleted=False,
    )

    target_party_id = uuid.uuid4().hex
    other_party_id = uuid.uuid4().hex

    db_session.add_all(
        [
            matter,
            Party(party_id=target_party_id, name_display="Target Staff"),
            PartyStaff(party_id=target_party_id, staff_code="target", dept="Dept-B", active=1),
            Party(party_id=other_party_id, name_display="Other Staff"),
            PartyStaff(party_id=other_party_id, staff_code="other", dept="Dept-A", active=1),
            MatterStaffAssignment(
                matter_id=matter_id,
                staff_party_id=other_party_id,
                staff_role_code="handler",
            ),
            User(
                username=f"target_{uuid.uuid4().hex[:6]}",
                email=f"target_{uuid.uuid4().hex[:6]}@example.com",
                role="patent_staff",
                is_active=True,
                department="Dept-B",
                staff_party_id=target_party_id,
            ),
        ]
    )
    db_session.commit()

    target_user = User.query.filter_by(staff_party_id=target_party_id).first()
    assert target_user is not None
    target_user_id = int(target_user.id)

    res_blocked = admin_client.get(
        "/admin/api/access-debug",
        query_string={"user": str(target_user_id), "matter": matter_id, "action": "view"},
        headers=req_headers,
    )
    assert res_blocked.status_code == 200
    payload_blocked = res_blocked.get_json()
    assert payload_blocked.get("ok") is True
    assert payload_blocked["evaluation"]["allowed"] is False
    assert payload_blocked["evaluation"]["facts"]["direct_assigned"] is False
    assert payload_blocked["evaluation"]["facts"]["team_assigned"] is False
    assert "focus_party_id" in payload_blocked["shortcuts"].get("team_reassign_url", "")

    db_session.add(
        MatterStaffAssignment(
            matter_id=matter_id,
            staff_party_id=target_party_id,
            staff_role_code="handler",
        )
    )
    db_session.commit()

    res_allowed = admin_client.get(
        "/admin/api/access-debug",
        query_string={"user": str(target_user_id), "matter": matter_id, "action": "view"},
        headers=req_headers,
    )
    assert res_allowed.status_code == 200
    payload_allowed = res_allowed.get_json()
    assert payload_allowed["ok"] is True
    assert payload_allowed["evaluation"]["allowed"] is True
    assert payload_allowed["evaluation"]["facts"]["direct_assigned"] is True


def test_admin_access_debug_supports_user_only_lookup(admin_client, db_session, monkeypatch):
    from app.services.core.config_service import ConfigService

    monkeypatch.setenv("ADMIN_CIDR_ALLOWLIST", "127.0.0.1/32")
    ConfigService.clear_cache()
    req_headers = {
        "X-Forwarded-For": "127.0.0.1",
        "X-Forwarded-Proto": "https",
    }

    user = User(
        username=f"user_only_{uuid.uuid4().hex[:6]}",
        email=f"user_only_{uuid.uuid4().hex[:6]}@example.com",
        role="patent_staff",
        is_active=True,
        department="Dept-C",
        staff_party_id=None,
    )
    db_session.add(user)
    db_session.commit()
    user_id = int(user.id)

    res = admin_client.get(
        "/admin/api/access-debug",
        query_string={"user": str(user_id), "action": "view"},
        headers=req_headers,
    )
    assert res.status_code == 200

    payload = res.get_json()
    assert payload.get("ok") is True
    assert payload["user"]["id"] == user_id
    assert payload["matter"] is None
    assert payload["evaluation"] is None
    assert payload["assignments"] == []
    assert payload["shortcuts"]["team_reassign_url"]
    assert payload["shortcuts"]["users_admin_url"]
