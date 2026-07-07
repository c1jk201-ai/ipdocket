from __future__ import annotations

import uuid

from app.models.matter import Matter, MatterStaffAssignment
from app.models.party import PartyStaff


def _set_user_staff_context(
    db_session, user, *, staff_party_id: str | None, department: str | None
) -> None:
    user.staff_party_id = staff_party_id
    user.department = department
    db_session.add(user)
    db_session.commit()


def _seed_matter(db_session, *, matter_id: str, our_ref: str) -> None:
    db_session.add(Matter(matter_id=matter_id, our_ref=our_ref, matter_type="PATENT"))


def _seed_assignment(
    db_session, *, matter_id: str, staff_party_id: str, staff_role_code: str = "manager"
) -> None:
    db_session.add(
        MatterStaffAssignment(
            msa_id=uuid.uuid4().hex,
            matter_id=matter_id,
            staff_party_id=staff_party_id,
            staff_role_code=staff_role_code,
            raw_text="",
        )
    )


def test_todos_side_effects_does_not_auto_close_globally_on_get(
    app, authenticated_client, monkeypatch
):
    # Even when explicitly enabled, global auto-close must never run from a web request.
    app.config["TASK_TODO_SIDE_EFFECTS_ENABLED"] = True
    app.config["TASK_TODO_SIDE_EFFECTS_ALLOW_GET"] = True
    app.config["DEADLINE_AUTO_CLOSE_ENABLED"] = True

    called = {"count": 0}

    def _fake_auto_close_post_due_deadlines(*, matter_id=None, today=None, commit=False):
        called["count"] += 1
        called["matter_id"] = matter_id
        called["commit"] = commit
        return {"evaluated": 0, "closed": 0, "followups": 0}

    monkeypatch.setattr(
        "app.services.deadlines.mgmt_deadlines.auto_close_post_due_deadlines",
        _fake_auto_close_post_due_deadlines,
    )

    res = authenticated_client.get("/api/productivity/todos")
    assert res.status_code == 200
    assert called["count"] == 0


def test_todos_side_effects_does_not_auto_close_without_edit_permission(
    app, db_session, authenticated_client, sample_user, monkeypatch
):
    app.config["TASK_TODO_SIDE_EFFECTS_ENABLED"] = True
    app.config["TASK_TODO_SIDE_EFFECTS_ALLOW_GET"] = True
    app.config["DEADLINE_AUTO_CLOSE_ENABLED"] = True

    # View-only team access (dept match) but no direct assignment => edit_case should be denied.
    _set_user_staff_context(db_session, sample_user, staff_party_id="S1", department="D1")

    mid = uuid.uuid4().hex
    _seed_matter(db_session, matter_id=mid, our_ref=f"TST-{uuid.uuid4().hex[:6]}")
    db_session.add(PartyStaff(party_id="S2", dept="D1", active=1))
    _seed_assignment(db_session, matter_id=mid, staff_party_id="S2")
    db_session.commit()

    called = {"count": 0}

    def _fake_auto_close_post_due_deadlines(*, matter_id=None, today=None, commit=False):
        called["count"] += 1
        return {"evaluated": 0, "closed": 0, "followups": 0}

    monkeypatch.setattr(
        "app.services.deadlines.mgmt_deadlines.auto_close_post_due_deadlines",
        _fake_auto_close_post_due_deadlines,
    )

    res = authenticated_client.get(f"/api/productivity/todosNewmatter_id={mid}")
    assert res.status_code == 200
    assert called["count"] == 0


def test_todos_side_effects_allows_auto_close_with_edit_permission(
    app, db_session, authenticated_client, sample_user, monkeypatch
):
    app.config["TASK_TODO_SIDE_EFFECTS_ENABLED"] = True
    app.config["TASK_TODO_SIDE_EFFECTS_ALLOW_GET"] = True
    app.config["DEADLINE_AUTO_CLOSE_ENABLED"] = True

    _set_user_staff_context(db_session, sample_user, staff_party_id="S1", department=None)

    mid = uuid.uuid4().hex
    _seed_matter(db_session, matter_id=mid, our_ref=f"TST-{uuid.uuid4().hex[:6]}")
    _seed_assignment(db_session, matter_id=mid, staff_party_id="S1")
    db_session.commit()

    called = {"count": 0, "matter_id": None, "commit": None}

    def _fake_auto_close_post_due_deadlines(*, matter_id=None, today=None, commit=False):
        called["count"] += 1
        called["matter_id"] = matter_id
        called["commit"] = commit
        return {"evaluated": 0, "closed": 0, "followups": 0}

    monkeypatch.setattr(
        "app.services.deadlines.mgmt_deadlines.auto_close_post_due_deadlines",
        _fake_auto_close_post_due_deadlines,
    )

    res = authenticated_client.get(f"/api/productivity/todosNewmatter_id={mid}")
    assert res.status_code == 200
    assert called["count"] == 1
    assert called["matter_id"] == mid
    assert called["commit"] is True
