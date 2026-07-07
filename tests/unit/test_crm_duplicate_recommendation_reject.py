import json
import uuid


def test_admin_can_reject_duplicate_recommendation_from_crm_list(admin_client, db_session):
    from app.models.client import Client
    from app.models.system_config import SystemConfig

    SystemConfig.set_config("CRM_DUPLICATE_REJECTED_GROUPS_JSON", "[]")
    db_session.commit()

    c1 = Client(name="Text-A", email="dedupe.case+one@example.com")
    c2 = Client(name="Text-B", email="dedupe.case@example.com")
    db_session.add_all([c1, c2])
    db_session.commit()

    signature = f"ids:{min(int(c1.id), int(c2.id))},{max(int(c1.id), int(c2.id))}"

    before = admin_client.get("/crm/")
    html_before = before.data.decode("utf-8")
    assert f'name="group_signature" value="{signature}"' in html_before
    merge_before = admin_client.get("/crm/clients/merge")
    html_merge_before = merge_before.data.decode("utf-8")
    assert f'name="group_signature" value="{signature}"' in html_merge_before

    resp = admin_client.post(
        "/crm/clients/duplicates/reject",
        data={"group_signature": signature},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    html_after = resp.data.decode("utf-8")
    assert f'name="group_signature" value="{signature}"' not in html_after

    saved = SystemConfig.get_config("CRM_DUPLICATE_REJECTED_GROUPS_JSON", "[]") or "[]"
    parsed = json.loads(saved)
    assert signature in parsed


def test_non_admin_cannot_reject_duplicate_recommendation(limited_client):
    resp = limited_client.post(
        "/crm/clients/duplicates/reject",
        data={"group_signature": "ids:1,2"},
    )
    assert resp.status_code == 403


def test_rbac_admin_role_user_sees_reject_button(client, db_session):
    from app.models.client import Client
    from app.models.role import Role
    from app.models.system_config import SystemConfig
    from app.models.user import User

    SystemConfig.set_config("CRM_DUPLICATE_REJECTED_GROUPS_JSON", "[]")
    db_session.commit()

    admin_role = Role.query.filter_by(name="admin").first()
    if not admin_role:
        admin_role = Role(name="admin", description="admin role", permissions=[])
        db_session.add(admin_role)
        db_session.flush()

    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"rbac-admin-{suffix}",
        email=f"rbac-admin-{suffix}@example.com",
        role="",
        is_active=True,
    )
    user.roles.append(admin_role)
    db_session.add(user)

    c1 = Client(name=f"RBACText-{suffix}-A", email=f"rbac.dup.{suffix}+a@example.com")
    c2 = Client(name=f"RBACText-{suffix}-B", email=f"rbac.dup.{suffix}@example.com")
    db_session.add_all([c1, c2])
    db_session.commit()

    with client.session_transaction() as session:
        session["_user_id"] = user.id
        session["_fresh"] = True

    signature = f"ids:{min(int(c1.id), int(c2.id))},{max(int(c1.id), int(c2.id))}"
    resp = client.get("/crm/")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert f'name="group_signature" value="{signature}"' in html
