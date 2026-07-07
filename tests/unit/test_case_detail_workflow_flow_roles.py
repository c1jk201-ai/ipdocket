from __future__ import annotations

from types import SimpleNamespace


def _workflow_stub(
    *,
    category: str = "WORK",
    assignee_id: int | None = None,
    attorney_assignee_id: int | None = None,
    inspector_id: int | None = None,
    name: str = "Text Text",
):
    return SimpleNamespace(
        category=category,
        assignee_id=assignee_id,
        attorney_assignee_id=attorney_assignee_id,
        inspector_id=inspector_id,
        name=name,
    )


def test_case_view_flow_role_codes_owner_prefers_handler_role(monkeypatch) -> None:
    from app.blueprints.case.services import detail_context
    from app.utils.task_distribution_rules import DistributionDecision

    monkeypatch.setattr(
        detail_context,
        "resolve_distribution_decision",
        lambda **kwargs: DistributionDecision(distribute_to="owner"),
    )
    wf = _workflow_stub(category="WORK", assignee_id=11, attorney_assignee_id=11)

    assert detail_context._resolve_case_view_flow_role_codes(wf=wf, linked_docket_item=None) == (
        "handler",
    )


def test_case_view_flow_role_codes_all_staff_uses_assignment_display_order(monkeypatch) -> None:
    from app.blueprints.case.services import detail_context
    from app.utils.task_distribution_rules import DistributionDecision

    monkeypatch.setattr(
        detail_context,
        "resolve_distribution_decision",
        lambda **kwargs: DistributionDecision(distribute_to="all_staff"),
    )
    wf = _workflow_stub(
        category="WORK",
        assignee_id=101,
        attorney_assignee_id=102,
        inspector_id=103,
    )

    assert detail_context._resolve_case_view_flow_role_codes(wf=wf, linked_docket_item=None) == (
        "attorney",
        "manager",
        "handler",
    )
