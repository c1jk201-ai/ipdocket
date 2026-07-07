from __future__ import annotations


def test_case_detail_audit_section_uses_display_name(app, db_session):
    from app.blueprints.case.services.detail_context import _build_audit_section
    from app.models.case_audit_log import CaseAuditLog
    from app.models.user import User

    u = User(
        username="u1", email="u1@example.com", role="user", is_active=True, display_name="Text"
    )
    db_session.add(u)
    db_session.commit()

    db_session.add(
        CaseAuditLog(
            case_id="mid1",
            actor_user_id=u.id,
            action="PATCH",
            field_name="memo",
            old_value={"memo": "a"},
            new_value={"memo": "b"},
        )
    )
    db_session.commit()

    ctx = {"_mid_str": "mid1", "users": [u]}
    out = _build_audit_section(ctx)
    rows = out.get("case_audit_rows") or []
    assert rows
    assert rows[0].get("user_name") == "Text"
