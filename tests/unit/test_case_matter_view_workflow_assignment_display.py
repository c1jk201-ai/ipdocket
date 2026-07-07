from __future__ import annotations

import uuid

from bs4 import BeautifulSoup


def test_case_detail_workflow_card_orders_assignment_fields_and_marks_work_roles(
    authenticated_client, db_session, sample_user, sample_matter
) -> None:
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    user_id = int(getattr(sample_user, "_test_id", None) or sample_user.id)
    task_name = f"Text-{uuid.uuid4().hex[:8]}"

    db_session.add(
        Workflow(
            case_id=matter_id,
            name=task_name,
            status="Pending",
            category="WORK",
            assignee_id=user_id,
            attorney_assignee_id=user_id,
            created_by_id=user_id,
        )
    )
    db_session.commit()

    resp = authenticated_client.get(f"/case/{matter_id}")

    assert resp.status_code == 200
    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    card = next(
        (
            node
            for node in soup.select("article.workflow-card")
            if task_name in node.get_text(" ", strip=True)
        ),
        None,
    )
    assert card is not None

    staff_labels = [
        " ".join(label.get_text(" ", strip=True).split())
        for label in card.select(".workflow-card-grid .workflow-field > label")
        if any(
            role_name in label.get_text(" ", strip=True)
            for role_name in ("Responsible attorney", "Manager", "Handler")
        )
    ]

    assert staff_labels == [
        "Responsible attorney Task",
        "Manager Task",
        "Handler Task",
    ]
