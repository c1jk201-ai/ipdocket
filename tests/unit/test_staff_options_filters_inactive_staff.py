import app.services.core.staff_options as staff_options
from app.models.party import Party, PartyStaff
from app.models.user import User
from app.services.core.staff_options import (
    build_staff_assignment_lists,
    build_staff_owner_options,
    resolve_staff_party_id,
)


def test_build_staff_assignment_lists_excludes_inactive_staff(app, db_session):
    db_session.add_all(
        [
            PartyStaff(party_id="p_active", staff_code="u_active", dept=None, active=1),
            PartyStaff(party_id="p_inactive", staff_code="u_inactive", dept=None, active=0),
            PartyStaff(party_id="p_null", staff_code="u_null", dept=None, active=None),
        ]
    )
    db_session.add_all(
        [
            User(
                username="u_active",
                email="u_active@example.com",
                is_active=True,
                staff_party_id="p_active",
            ),
            # Staff directory deactivated -> must be excluded
            User(
                username="u_staff_deactivated",
                email="u_staff_deactivated@example.com",
                is_active=True,
                staff_party_id="p_inactive",
            ),
            # App user deactivated -> must be excluded
            User(
                username="u_user_inactive",
                email="u_user_inactive@example.com",
                is_active=False,
                staff_party_id="p_active",
            ),
            # Linked to missing staff directory row -> must be excluded
            User(
                username="u_staff_missing",
                email="u_staff_missing@example.com",
                is_active=True,
                staff_party_id="p_missing",
            ),
            # Unlinked users are allowed as a fallback
            User(
                username="u_unlinked",
                email="u_unlinked@example.com",
                is_active=True,
                staff_party_id=None,
            ),
            # Legacy PartyStaff.active=NULL treated as active
            User(
                username="u_null",
                email="u_null@example.com",
                is_active=True,
                staff_party_id="p_null",
            ),
        ]
    )
    db_session.commit()

    with app.app_context():
        lists = build_staff_assignment_lists()
        usernames = {u.username for u in (lists.get("all_users") or [])}

    assert "u_active" in usernames
    assert "u_null" in usernames
    # Prefer directory-linked users when available (avoid showing stale/unlinked accounts)
    assert "u_unlinked" not in usernames
    assert "u_staff_deactivated" not in usernames
    assert "u_user_inactive" not in usernames
    assert "u_staff_missing" not in usernames


def test_build_staff_assignment_lists_domain_misconfig_fallback(app, db_session, monkeypatch):
    db_session.add(PartyStaff(party_id="p1", staff_code="u1", dept=None, active=1))
    db_session.add(
        User(
            username="u1",
            email="u1@example.com",
            is_active=True,
            staff_party_id="p1",
        )
    )
    db_session.commit()

    # Domain filter excludes u1, so the function should fall back to "no domain" to avoid empty pickers.
    monkeypatch.setitem(app.config, "STAFF_EMAIL_DOMAINS", "no-match.invalid")
    with app.app_context():
        lists = build_staff_assignment_lists()
        usernames = [u.username for u in (lists.get("all_users") or [])]

    assert usernames == ["u1"]


def test_build_staff_assignment_lists_includes_unlinked_when_no_linked_users(app, db_session):
    db_session.add(
        User(
            username="u_only",
            email="u_only@example.com",
            is_active=True,
            staff_party_id=None,
        )
    )
    db_session.commit()

    with app.app_context():
        lists = build_staff_assignment_lists()
        usernames = [u.username for u in (lists.get("all_users") or [])]

    assert usernames == ["u_only"]


def test_build_staff_assignment_lists_separates_attorney_and_processing_roles(
    app, db_session, monkeypatch
):
    db_session.add_all(
        [
            PartyStaff(party_id="p_attorney", staff_code="attorney_1", dept=None, active=1),
            PartyStaff(party_id="p_partner", staff_code="partner_1", dept=None, active=1),
            PartyStaff(party_id="p_staff", staff_code="staff_1", dept=None, active=1),
        ]
    )
    db_session.add_all(
        [
            User(
                username="attorney_1",
                email="attorney_1@example.com",
                role="lead_attorney",
                is_active=True,
                staff_party_id="p_attorney",
            ),
            User(
                username="partner_1",
                email="partner_1@example.com",
                role="partner_attorney",
                is_active=True,
                staff_party_id="p_partner",
            ),
            User(
                username="staff_1",
                email="staff_1@example.com",
                role="patent_staff",
                is_active=True,
                staff_party_id="p_staff",
            ),
        ]
    )
    db_session.commit()

    def fake_get_str(key, default=None, *, strip=True, allow_blank=True, prefer_env=False):
        if key == "STAFF_PROFESSIONAL_ROLES":
            return None
        if key == "STAFF_ATTORNEY_ROLES":
            return "lead_attorney,partner_attorney"
        return default

    monkeypatch.setattr(staff_options.ConfigService, "get_str", staticmethod(fake_get_str))

    with app.app_context():
        lists = build_staff_assignment_lists()

    attorney_usernames = [u.username for u in (lists.get("attorney_users") or [])]
    professional_usernames = [u.username for u in (lists.get("professional_users") or [])]
    processing_usernames = [u.username for u in (lists.get("processing_users") or [])]

    assert attorney_usernames == ["attorney_1", "partner_1"]
    assert professional_usernames == attorney_usernames
    assert processing_usernames == ["staff_1"]


def test_build_staff_assignment_lists_uses_staff_professional_roles_config(
    app, db_session, monkeypatch
):
    db_session.add(PartyStaff(party_id="p_admin", staff_code="jyjung", dept=None, active=1))
    db_session.add(
        User(
            username="jyjung",
            email="jyjung@example.com",
            role="admin",
            is_active=True,
            staff_party_id="p_admin",
        )
    )
    db_session.commit()

    def fake_get_str(key, default=None, *, strip=True, allow_blank=True, prefer_env=False):
        if key == "STAFF_PROFESSIONAL_ROLES":
            return "admin"
        return default

    monkeypatch.setattr(staff_options.ConfigService, "get_str", staticmethod(fake_get_str))

    with app.app_context():
        lists = build_staff_assignment_lists()

    attorney_usernames = [u.username for u in (lists.get("attorney_users") or [])]
    professional_usernames = [u.username for u in (lists.get("professional_users") or [])]

    assert attorney_usernames == ["jyjung"]
    assert professional_usernames == ["jyjung"]


def test_build_staff_assignment_lists_falls_back_to_legacy_staff_attorney_roles_config(
    app, db_session, monkeypatch
):
    db_session.add(PartyStaff(party_id="p_admin", staff_code="jyjung", dept=None, active=1))
    db_session.add(
        User(
            username="jyjung",
            email="jyjung@example.com",
            role="admin",
            is_active=True,
            staff_party_id="p_admin",
        )
    )
    db_session.commit()

    def fake_get_str(key, default=None, *, strip=True, allow_blank=True, prefer_env=False):
        if key == "STAFF_PROFESSIONAL_ROLES":
            return None
        if key == "STAFF_ATTORNEY_ROLES":
            return "admin"
        return default

    monkeypatch.setattr(staff_options.ConfigService, "get_str", staticmethod(fake_get_str))

    with app.app_context():
        lists = build_staff_assignment_lists()

    attorney_usernames = [u.username for u in (lists.get("attorney_users") or [])]
    professional_usernames = [u.username for u in (lists.get("professional_users") or [])]

    assert attorney_usernames == ["jyjung"]
    assert professional_usernames == ["jyjung"]


def test_build_staff_owner_options_use_user_directory_entries(app, db_session):
    db_session.add_all(
        [
            Party(party_id="p_jyjung", name_display="Text", party_kind="staff"),
            Party(party_id="p_mhkang", name_display="Text", party_kind="staff"),
            Party(party_id="p_system", name_display="Text", party_kind="staff"),
            PartyStaff(party_id="p_jyjung", staff_code="jyjung", dept=None, active=1),
            PartyStaff(party_id="p_mhkang", staff_code="mhkang", dept=None, active=1),
            PartyStaff(party_id="p_system", staff_code="system_record", dept=None, active=1),
        ]
    )
    db_session.add_all(
        [
            User(
                username="jyjung",
                display_name="Text",
                email="jyjung@example.com",
                is_active=True,
                staff_party_id="p_jyjung",
                role="admin",
            ),
            User(
                username="mhkang",
                display_name="Text",
                email="mhkang@example.com",
                is_active=True,
                staff_party_id="p_mhkang",
                role="patent_staff",
            ),
        ]
    )
    db_session.commit()

    with app.app_context():
        options = build_staff_owner_options(category="all")

    values = [opt["value"] for opt in options]
    assert "Text[jyjung]" in values
    assert "Text[mhkang]" in values
    assert "Text[system_record]" not in values


def test_resolve_staff_party_id_accepts_picker_value_username_and_email(app, db_session):
    db_session.add_all(
        [
            Party(party_id="p_jyjung", name_display="Text", party_kind="staff"),
            PartyStaff(party_id="p_jyjung", staff_code="jyjung", dept=None, active=1),
            User(
                username="jyjung",
                display_name="Text",
                email="jyjung@example.com",
                is_active=True,
                staff_party_id="p_jyjung",
                role="admin",
            ),
        ]
    )
    db_session.commit()

    with app.app_context():
        assert resolve_staff_party_id("p_jyjung") == "p_jyjung"
        assert resolve_staff_party_id("jyjung") == "p_jyjung"
        assert resolve_staff_party_id("Text") == "p_jyjung"
        assert resolve_staff_party_id("Text[jyjung]") == "p_jyjung"
        assert resolve_staff_party_id("Text(jyjung)") == "p_jyjung"
        assert resolve_staff_party_id("jyjung@example.com") == "p_jyjung"
