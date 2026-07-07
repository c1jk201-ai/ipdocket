from __future__ import annotations

import uuid

from app.models.matter import Family, Matter, MatterFamily, MatterStaffAssignment
from app.models.user import User
from app.utils.permissions import can_access_matter


def _new_matter() -> Matter:
    return Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"SELF-{uuid.uuid4().hex[:8]}",
        right_name="Self assigned scope test",
        is_deleted=False,
    )


def test_self_assigned_case_allows_manager_attorney_handler_roles(app, db_session):
    user = User(
        username=f"self_scope_{uuid.uuid4().hex[:6]}",
        email=f"self_scope_{uuid.uuid4().hex[:6]}@example.com",
        role="patent_staff",
        is_active=True,
        staff_party_id=f"staff_{uuid.uuid4().hex[:8]}",
    )
    db_session.add(user)
    db_session.flush()

    for role_code in ("manager", "attorney", "handler"):
        matter = _new_matter()
        db_session.add(matter)
        db_session.flush()
        db_session.add(
            MatterStaffAssignment(
                matter_id=str(matter.matter_id),
                staff_party_id=user.staff_party_id,
                staff_role_code=role_code,
            )
        )
        db_session.flush()
        assert can_access_matter(user, str(matter.matter_id), action="view") is True


def test_self_assigned_case_excludes_non_case_view_roles_by_default(app, db_session):
    user = User(
        username=f"self_scope_ex_{uuid.uuid4().hex[:6]}",
        email=f"self_scope_ex_{uuid.uuid4().hex[:6]}@example.com",
        role="patent_staff",
        is_active=True,
        staff_party_id=f"staff_{uuid.uuid4().hex[:8]}",
    )
    matter = _new_matter()
    db_session.add_all([user, matter])
    db_session.flush()
    db_session.add(
        MatterStaffAssignment(
            matter_id=str(matter.matter_id),
            staff_party_id=user.staff_party_id,
            staff_role_code="draftsman",
        )
    )
    db_session.commit()

    assert can_access_matter(user, str(matter.matter_id), action="view") is False


def test_self_assigned_case_role_codes_can_be_extended_by_config(app, db_session, monkeypatch):
    user = User(
        username=f"self_scope_cfg_{uuid.uuid4().hex[:6]}",
        email=f"self_scope_cfg_{uuid.uuid4().hex[:6]}@example.com",
        role="patent_staff",
        is_active=True,
        staff_party_id=f"staff_{uuid.uuid4().hex[:8]}",
    )
    matter = _new_matter()
    db_session.add_all([user, matter])
    db_session.flush()
    db_session.add(
        MatterStaffAssignment(
            matter_id=str(matter.matter_id),
            staff_party_id=user.staff_party_id,
            staff_role_code="draftsman",
        )
    )
    db_session.commit()

    from app.utils import permissions as permission_utils

    original_get_str = permission_utils.ConfigService.get_str

    def _patched_get_str(key, default=None, **kwargs):
        if key == "CASE_SELF_ASSIGNED_ROLE_CODES":
            return "manager,attorney,handler,draftsman"
        return original_get_str(key, default, **kwargs)

    monkeypatch.setattr(permission_utils.ConfigService, "get_str", _patched_get_str)
    permission_utils.ConfigService.clear_cache()

    assert can_access_matter(user, str(matter.matter_id), action="view") is True


def test_family_linked_case_inherits_view_only_access_from_assigned_sibling(app, db_session):
    user = User(
        username=f"self_scope_family_{uuid.uuid4().hex[:6]}",
        email=f"self_scope_family_{uuid.uuid4().hex[:6]}@example.com",
        role="patent_staff",
        is_active=True,
        staff_party_id=f"staff_{uuid.uuid4().hex[:8]}",
    )
    source = _new_matter()
    sibling = _new_matter()
    family = Family(
        family_id=uuid.uuid4().hex,
        family_key=f"FAM-{uuid.uuid4().hex[:6].upper()}",
    )
    db_session.add_all([user, source, sibling, family])
    db_session.flush()
    db_session.add_all(
        [
            MatterStaffAssignment(
                matter_id=str(source.matter_id),
                staff_party_id=user.staff_party_id,
                staff_role_code="manager",
            ),
            MatterFamily(
                mf_id=uuid.uuid4().hex,
                matter_id=str(source.matter_id),
                family_id=family.family_id,
            ),
            MatterFamily(
                mf_id=uuid.uuid4().hex,
                matter_id=str(sibling.matter_id),
                family_id=family.family_id,
            ),
        ]
    )
    db_session.commit()

    assert can_access_matter(user, str(source.matter_id), action="view") is True
    assert can_access_matter(user, str(sibling.matter_id), action="view") is True
    assert can_access_matter(user, str(sibling.matter_id), action="edit_case") is False
