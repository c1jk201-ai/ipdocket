import pytest


def test_apply_field_matter_whitelist_blocks_injection(app, db_session, sample_matter):
    from app.models.ip_records import Matter
    from app.services.parameter_conflict.parameter_conflict_types import ConflictItem
    from app.services.parameter_conflict.parameter_conflict_updater import apply_field

    # Text: right_name Text Text
    ok = ConflictItem(
        field_name="right_name",
        field_label="Text Text",
        current_value=None,
        new_value="Text Text",
        table_name="matter",
        field_key="right_name",
        priority=1,
    )
    apply_field(
        matter_id=str(sample_matter.matter_id),
        item=ok,
        get_custom_field_namespace=lambda: "domestic_patent",
    )
    db_session.commit()
    m = Matter.query.get(str(sample_matter.matter_id))
    assert (m.right_name or "") == "Text Text"

    # Text: field_key Text Text Text
    bad = ConflictItem(
        field_name="right_name",
        field_label="Text Text",
        current_value=None,
        new_value="X",
        table_name="matter",
        field_key="right_name = 'HACK', status_red = 'PWNED' --",
        priority=1,
    )
    with pytest.raises(ValueError):
        apply_field(
            matter_id=str(sample_matter.matter_id),
            item=bad,
            get_custom_field_namespace=lambda: "domestic_patent",
        )


def test_apply_field_staff_assignment_uses_staff_party_id_not_user_id(
    app, db_session, sample_matter
):
    import uuid

    from app.models.ip_records import MatterStaffAssignment
    from app.models.user import User
    from app.services.parameter_conflict.parameter_conflict_types import ConflictItem
    from app.services.parameter_conflict.parameter_conflict_updater import apply_field

    # Text: display_nameText Text Text + staff_party_id Text
    staff_pid = f"party_{uuid.uuid4().hex}"
    u = User(
        username=f"u_{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Text",
        staff_party_id=staff_pid,
        role="user",
        is_active=True,
    )
    db_session.add(u)
    db_session.commit()

    item = ConflictItem(
        field_name="attorney",
        field_label="Text",
        current_value=None,
        new_value="Text",
        table_name="matter_staff_assignment",
        field_key="attorney",
        priority=3,
    )
    apply_field(
        matter_id=str(sample_matter.matter_id),
        item=item,
        get_custom_field_namespace=lambda: "domestic_patent",
    )
    db_session.commit()

    row = (
        MatterStaffAssignment.query.filter_by(
            matter_id=str(sample_matter.matter_id),
            staff_role_code="attorney",
        )
        .order_by(MatterStaffAssignment.msa_id.desc())
        .first()
    )
    assert row is not None
    assert row.staff_party_id == staff_pid
