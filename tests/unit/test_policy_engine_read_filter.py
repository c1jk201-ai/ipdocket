import uuid


def test_policy_engine_filters_matter_queries_by_assignment(app, db_session):
    from flask_login import login_user, logout_user

    from app.models.ip_records import Matter, MatterStaffAssignment
    from app.models.user import User

    user = User(
        username="policy_user",
        email="policy_user@example.com",
        role="user",
        staff_party_id="staff_u1",
        is_active=True,
    )
    db_session.add(user)

    m1 = Matter(matter_id=uuid.uuid4().hex, our_ref="POLICY-REF-1", right_name="Matter 1")
    m2 = Matter(matter_id=uuid.uuid4().hex, our_ref="POLICY-REF-2", right_name="Matter 2")
    db_session.add_all([m1, m2])
    db_session.flush()

    db_session.add_all(
        [
            MatterStaffAssignment(
                matter_id=m1.matter_id,
                staff_party_id="staff_u1",
                staff_role_code="attorney",
            ),
            MatterStaffAssignment(
                matter_id=m2.matter_id,
                staff_party_id="staff_u2",
                staff_role_code="attorney",
            ),
        ]
    )
    db_session.commit()

    with app.test_request_context("/"):
        login_user(user)
        refs = [m.our_ref for m in Matter.query.order_by(Matter.our_ref.asc()).all()]
        logout_user()

    assert refs == ["POLICY-REF-1"]


def test_policy_engine_team_scope_includes_department(app, db_session):
    from flask_login import login_user, logout_user

    from app.models.party import PartyStaff
    from app.models.ip_records import Matter, MatterStaffAssignment
    from app.models.user import User

    user = User(
        username="policy_team_user",
        email="policy_team_user@example.com",
        role="patent_staff",
        staff_party_id="staff_team_u1",
        department="DEPT-A",
        is_active=True,
    )
    db_session.add(user)

    db_session.add(PartyStaff(party_id="staff_team_u2", dept="DEPT-A", active=1))

    m1 = Matter(matter_id=uuid.uuid4().hex, our_ref="POLICY-TEAM-REF-1", right_name="Matter 1")
    m2 = Matter(matter_id=uuid.uuid4().hex, our_ref="POLICY-TEAM-REF-2", right_name="Matter 2")
    db_session.add_all([m1, m2])
    db_session.flush()

    # Direct assignment + same-dept team assignment
    db_session.add_all(
        [
            MatterStaffAssignment(
                matter_id=m1.matter_id,
                staff_party_id="staff_team_u1",
                staff_role_code="attorney",
            ),
            MatterStaffAssignment(
                matter_id=m2.matter_id,
                staff_party_id="staff_team_u2",
                staff_role_code="attorney",
            ),
        ]
    )
    db_session.commit()

    with app.test_request_context("/"):
        login_user(user)
        refs = [m.our_ref for m in Matter.query.order_by(Matter.our_ref.asc()).all()]
        logout_user()

    assert refs == ["POLICY-TEAM-REF-1", "POLICY-TEAM-REF-2"]
