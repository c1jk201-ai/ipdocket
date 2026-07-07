from __future__ import annotations

from datetime import timedelta


def test_capacity_planner_counts_assigned_workflow(
    authenticated_client,
    sample_user,
    sample_matter,
    db_session,
):
    from app.models.workflow import Workflow
    from app.utils.timezone import today_local

    matter_id = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)
    user_id = getattr(sample_user, "_test_id", None) or sample_user.id
    db_session.add(
        Workflow(
            case_id=matter_id,
            name="Capacity sample task",
            status=Workflow.STATUS_PENDING,
            due_date=today_local() + timedelta(days=7),
            assignee_id=user_id,
            work_hours=3.5,
        )
    )
    db_session.commit()

    res = authenticated_client.get("/api/productivity/capacity-plannerNewwindows=14")
    assert res.status_code == 200
    data = res.get_json() or {}
    assert data["ok"] is True
    window = data["windows"][0]
    assert window["days"] == 14
    assert window["total_items"] == 1
    assert window["total_estimated_hours"] == 3.5
    assert window["people"][0]["item_count"] == 1
    assert window["people"][0]["estimated_hours"] == 3.5
