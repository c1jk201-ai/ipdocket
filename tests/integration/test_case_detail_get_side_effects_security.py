from __future__ import annotations

import uuid


def test_case_detail_get_does_not_commit_or_mutate_auto_status_for_view_only_user(
    authenticated_client, sample_user, db_session, monkeypatch
):
    """
    P1 regression:
    - /case/<id> is a GET endpoint guarded by "view" permission.
    - It must not write/commit self-heal changes (status_* / matter_event backfills).
    """
    from app.models.party import PartyStaff
    from app.models.ip_records import Matter, MatterStaffAssignment, OfficeAction, VMatterOverview

    # Create a user that can VIEW via team assignment, but cannot EDIT (no direct assignment and not team lead).
    sample_user = db_session.merge(sample_user)
    if not (sample_user.department or "").strip():
        sample_user.department = f"dept_{uuid.uuid4().hex[:6]}"
        db_session.add(sample_user)
        db_session.flush()

    matter_id = uuid.uuid4().hex
    our_ref = f"TEST-GET-NO-COMMIT-{matter_id[:8]}"
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="GET self-heal security test",
            right_group="DOM",
            matter_type="PATENT",
            status_red="",  # derived red should differ -> would trigger self-heal in old behavior
            status_red_related_date="",
            status_blue="",
            is_deleted=False,
        )
    )
    db_session.add(
        VMatterOverview(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="GET self-heal security test",
            right_group="DOM",
            matter_type="PATENT",
            applicants="",
            clients="",
            attorneys="",
            entered_at="2026-01-01",
        )
    )

    # Team assignment: some staff in the same dept is assigned to the matter.
    team_staff_pid = f"party_{uuid.uuid4().hex[:8]}"
    db_session.add(PartyStaff(party_id=team_staff_pid, dept=sample_user.department, active=1))
    db_session.add(
        MatterStaffAssignment(
            matter_id=matter_id,
            staff_party_id=team_staff_pid,
            staff_role_code="attorney",
        )
    )
    db_session.add(
        OfficeAction(
            oa_id=uuid.uuid4().hex,
            matter_id=matter_id,
            doc_name="Text",
            received_date="2026-02-06",
            due_date="2026-03-06",
            done_date=None,
        )
    )
    db_session.commit()

    # Spy on commit: the old buggy implementation committed during GET.
    import app.blueprints.case.services.detail_context as detail_context

    commits: list[None] = []

    def _spy_commit():  # noqa: ANN001
        commits.append(None)
        return None

    monkeypatch.setattr(detail_context.db.session, "commit", _spy_commit)

    resp = authenticated_client.get(f"/case/{matter_id}")
    assert resp.status_code == 200
    assert len(commits) == 0

    # Ensure persisted status fields were not modified as a side effect of GET.
    db_session.expire_all()
    matter = Matter.query.get(matter_id)
    assert matter is not None
    assert (matter.status_red or "") == ""
    assert (matter.status_red_related_date or "") == ""
