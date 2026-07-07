from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.utils import permissions


class _DummyUser(SimpleNamespace):
    def has_permission(self, perm: str) -> bool:
        if "admin" in self.role_names:
            return True
        for role_obj in self.roles or []:
            if perm in (getattr(role_obj, "permissions", None) or []):
                return True
        return False

    @property
    def role_names(self) -> set[str]:
        names = set()
        for role_obj in self.roles or []:
            role_name = str(getattr(role_obj, "name", "") or "").strip().lower()
            if role_name:
                names.add(role_name)
        for item in str(getattr(self, "role", "") or "").split(","):
            role_name = item.strip().lower()
            if role_name:
                names.add(role_name)
        return names


def _user(*, role: str = "user", roles: list | None = None):
    return _DummyUser(
        is_authenticated=True,
        is_active=True,
        role=role,
        roles=roles or [],
        staff_party_id="staff-1",
        department="dept-1",
    )


def test_pick_primary_role_name_prefers_admin_priority():
    primary = permissions.pick_primary_role_name(["patent_staff", "admin"], default="user")
    assert primary == "admin"


def test_can_access_matter_uses_explicit_case_view_all(monkeypatch):
    monkeypatch.setattr(permissions, "_has_direct_assignment", lambda **_: False)
    monkeypatch.setattr(permissions, "_has_team_assignment", lambda **_: False)

    role_obj = SimpleNamespace(
        name="case_global_viewer",
        permissions=[permissions.PERM_CASE_VIEW_ALL],
    )
    user = _user(role="patent_staff", roles=[role_obj])

    assert permissions.can_access_matter(user, "M-1", action="view") is True


def test_can_access_matter_explicit_assigned_requires_direct_assignment(monkeypatch):
    role_obj = SimpleNamespace(
        name="assigned_only",
        permissions=[permissions.PERM_CASE_VIEW_ASSIGNED],
    )
    user = _user(role="patent_staff", roles=[role_obj])

    monkeypatch.setattr(permissions, "_has_direct_assignment", lambda **_: False)
    monkeypatch.setattr(permissions, "_has_team_assignment", lambda **_: True)
    assert permissions.can_access_matter(user, "M-2", action="view") is False

    monkeypatch.setattr(permissions, "_has_direct_assignment", lambda **_: True)
    assert permissions.can_access_matter(user, "M-2", action="view") is True


def test_can_access_matter_legacy_multi_roles_allows_non_readonly_edit(monkeypatch):
    monkeypatch.setattr(permissions, "_has_direct_assignment", lambda **_: True)
    monkeypatch.setattr(permissions, "_has_team_assignment", lambda **_: False)

    # Legacy fallback path (no explicit case.* permissions):
    # mixed roles should not be blocked just because 'user' is present.
    user = _user(role="user,patent_staff", roles=[])

    assert permissions.can_access_matter(user, "M-3", action="edit_case") is True


@pytest.mark.parametrize("role_name", ["lead_attorney", "partner_attorney"])
def test_can_access_matter_super_attorney_has_explicit_view_and_assign(monkeypatch, role_name):
    monkeypatch.setattr(permissions, "_has_direct_assignment", lambda **_: False)
    monkeypatch.setattr(permissions, "_has_team_assignment", lambda **_: False)

    role_obj = SimpleNamespace(
        name=role_name,
        permissions=[permissions.PERM_CASE_VIEW_ALL, permissions.PERM_CASE_ASSIGN_ALL],
    )
    user = _user(role=role_name, roles=[role_obj])

    assert permissions.can_access_matter(user, "M-4", action="view") is True
    assert permissions.can_access_matter(user, "M-4", action="assign_staff") is True


def test_parse_role_csv_normalizes_and_deduplicates():
    parsed = permissions._parse_role_csv(" Admin, manager,MANAGER, , patent_staff ")
    assert parsed == {"admin", "manager", "patent_staff"}


def test_normalize_role_codes_sorts_and_filters_blanks():
    normalized = permissions._normalize_role_codes([" handler ", "", None, "Attorney", "attorney"])
    assert normalized == ("attorney", "handler")


def test_can_access_uploads_honors_allowed_roles_config(app):
    user = _user(role="custom_ops", roles=[])

    with app.app_context():
        app.config["UPLOADS_ALLOWED_ROLES"] = " custom_ops,other_role "
        assert permissions.can_access_uploads(user) is True


def test_require_permission_respects_login_disabled_in_testing(app, monkeypatch):
    with app.app_context():
        monkeypatch.setitem(app.config, "LOGIN_DISABLED", True)
        monkeypatch.setattr(permissions, "current_user", SimpleNamespace(is_authenticated=False))
        wrapped = permissions.require_permission("manage_case")(lambda: "ok")
        assert wrapped() == "ok"


def test_can_access_matter_allows_family_related_view_but_not_edit(db_session):
    from app.models.ip_records import Matter, MatterFamily, MatterStaffAssignment

    family_id = uuid.uuid4().hex
    target = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"FAM-{uuid.uuid4().hex[:6]}-A",
        right_name="Series target",
        is_deleted=False,
    )
    sibling = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"FAM-{uuid.uuid4().hex[:6]}-B",
        right_name="Series sibling",
        is_deleted=False,
    )
    db_session.add_all([target, sibling])
    db_session.flush()
    db_session.add_all(
        [
            MatterFamily(
                mf_id=uuid.uuid4().hex,
                matter_id=str(target.matter_id),
                family_id=family_id,
            ),
            MatterFamily(
                mf_id=uuid.uuid4().hex,
                matter_id=str(sibling.matter_id),
                family_id=family_id,
            ),
            MatterStaffAssignment(
                matter_id=str(sibling.matter_id),
                staff_party_id="staff-1",
                staff_role_code="manager",
            ),
        ]
    )
    db_session.commit()

    user = _user(role="patent_staff")

    assert permissions.can_access_matter(user, str(target.matter_id), action="view") is True
    assert permissions.can_access_matter(user, str(target.matter_id), action="edit_case") is False


def test_can_access_matter_ignores_deleted_family_related_assignment(db_session):
    from app.models.ip_records import Matter, MatterFamily, MatterStaffAssignment

    family_id = uuid.uuid4().hex
    target = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"FAM-DEL-{uuid.uuid4().hex[:6]}-A",
        right_name="Active target",
        is_deleted=False,
    )
    deleted_sibling = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"FAM-DEL-{uuid.uuid4().hex[:6]}-B",
        right_name="Deleted sibling",
        is_deleted=True,
    )
    db_session.add_all([target, deleted_sibling])
    db_session.flush()
    db_session.add_all(
        [
            MatterFamily(
                mf_id=uuid.uuid4().hex,
                matter_id=str(target.matter_id),
                family_id=family_id,
            ),
            MatterFamily(
                mf_id=uuid.uuid4().hex,
                matter_id=str(deleted_sibling.matter_id),
                family_id=family_id,
            ),
            MatterStaffAssignment(
                matter_id=str(deleted_sibling.matter_id),
                staff_party_id="staff-1",
                staff_role_code="manager",
            ),
        ]
    )
    db_session.commit()

    user = _user(role="patent_staff")

    assert permissions.can_access_matter(user, str(target.matter_id), action="view") is False


def test_can_access_matter_explicit_family_view_honors_assigned_permission(db_session):
    from app.models.ip_records import Matter, MatterFamily, MatterStaffAssignment

    family_id = uuid.uuid4().hex
    target = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"EXP-{uuid.uuid4().hex[:6]}-A",
        right_name="Explicit target",
        is_deleted=False,
    )
    sibling = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"EXP-{uuid.uuid4().hex[:6]}-B",
        right_name="Explicit sibling",
        is_deleted=False,
    )
    db_session.add_all([target, sibling])
    db_session.flush()
    db_session.add_all(
        [
            MatterFamily(
                mf_id=uuid.uuid4().hex,
                matter_id=str(target.matter_id),
                family_id=family_id,
            ),
            MatterFamily(
                mf_id=uuid.uuid4().hex,
                matter_id=str(sibling.matter_id),
                family_id=family_id,
            ),
            MatterStaffAssignment(
                matter_id=str(sibling.matter_id),
                staff_party_id="staff-1",
                staff_role_code="attorney",
            ),
        ]
    )
    db_session.commit()

    role_obj = SimpleNamespace(
        name="assigned_only", permissions=[permissions.PERM_CASE_VIEW_ASSIGNED]
    )
    user = _user(role="patent_staff", roles=[role_obj])

    assert permissions.can_access_matter(user, str(target.matter_id), action="view") is True


def test_can_access_matter_allows_identifier_related_view_but_not_edit(db_session):
    from app.models.ip_records import Matter, MatterCustomField, MatterIdentifier, MatterStaffAssignment

    target = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"REL-{uuid.uuid4().hex[:6]}-A",
        right_name="Identifier target",
        is_deleted=False,
    )
    sibling = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"REL-{uuid.uuid4().hex[:6]}-B",
        right_name="Identifier sibling",
        is_deleted=False,
    )
    db_session.add_all([target, sibling])
    db_session.flush()
    db_session.add_all(
        [
            MatterCustomField(
                matter_id=str(target.matter_id),
                namespace="pct",
                data={"priority_no": "10-2026-0001234"},
            ),
            MatterIdentifier(
                matter_id=str(sibling.matter_id),
                id_type="Priority",
                id_value="10-2026-0001234",
            ),
            MatterStaffAssignment(
                matter_id=str(sibling.matter_id),
                staff_party_id="staff-1",
                staff_role_code="manager",
            ),
        ]
    )
    db_session.commit()

    user = _user(role="patent_staff")

    assert permissions.can_access_matter(user, str(target.matter_id), action="view") is True
    assert permissions.can_access_matter(user, str(target.matter_id), action="edit_case") is False


def test_can_access_matter_explicit_identifier_view_honors_assigned_permission(db_session):
    from app.models.ip_records import Matter, MatterIdentifier, MatterStaffAssignment

    target = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"IDEXP-{uuid.uuid4().hex[:6]}-A",
        right_name="Explicit identifier target",
        is_deleted=False,
    )
    sibling = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"IDEXP-{uuid.uuid4().hex[:6]}-B",
        right_name="Explicit identifier sibling",
        is_deleted=False,
    )
    db_session.add_all([target, sibling])
    db_session.flush()
    db_session.add_all(
        [
            MatterIdentifier(
                matter_id=str(target.matter_id),
                id_type="PCT Application No.",
                id_value="PCT/US2026/000777",
            ),
            MatterIdentifier(
                matter_id=str(sibling.matter_id),
                id_type="PCT Application No.",
                id_value="PCT/US2026/000777",
            ),
            MatterStaffAssignment(
                matter_id=str(sibling.matter_id),
                staff_party_id="staff-1",
                staff_role_code="attorney",
            ),
        ]
    )
    db_session.commit()

    role_obj = SimpleNamespace(
        name="assigned_only", permissions=[permissions.PERM_CASE_VIEW_ASSIGNED]
    )
    user = _user(role="patent_staff", roles=[role_obj])

    assert permissions.can_access_matter(user, str(target.matter_id), action="view") is True
