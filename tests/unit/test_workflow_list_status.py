from datetime import date

from app.utils.workflow_list_status import (
    compute_workflow_list_status,
    is_workflow_terminal_status,
    normalize_workflow_status_key,
)


def test_compute_workflow_list_status_terminal_statuses_win() -> None:
    today = date(2026, 2, 24)
    assert (
        compute_workflow_list_status(status="Completed", due_date=today, today=today) == "completed"
    )
    assert (
        compute_workflow_list_status(status="Abandoned", due_date=today, today=today) == "abandoned"
    )
    assert (
        compute_workflow_list_status(status=" completed ", due_date=today, today=today)
        == "completed"
    )
    assert compute_workflow_list_status(status="done", due_date=today, today=today) == "completed"
    assert (
        compute_workflow_list_status(status="cancelled", due_date=today, today=today) == "abandoned"
    )


def test_workflow_status_helpers_normalize_legacy_values() -> None:
    assert normalize_workflow_status_key("IN_PROGRESS") == "in progress"
    assert normalize_workflow_status_key("in-progress") == "in progress"
    assert is_workflow_terminal_status("DONE")
    assert is_workflow_terminal_status(" canceled ")
    assert not is_workflow_terminal_status(None)
    assert not is_workflow_terminal_status("Pending")


def test_compute_workflow_list_status_overdue_and_urgent() -> None:
    today = date(2026, 2, 24)
    assert (
        compute_workflow_list_status(
            status="Pending", due_date=date(2026, 2, 23), today=today, urgent_window_days=7
        )
        == "overdue"
    )
    assert (
        compute_workflow_list_status(
            status="In Progress", due_date=date(2026, 2, 24), today=today, urgent_window_days=7
        )
        == "urgent"
    )
    assert (
        compute_workflow_list_status(
            status="Pending", due_date=date(2026, 3, 3), today=today, urgent_window_days=7
        )
        == "urgent"
    )
    assert (
        compute_workflow_list_status(
            status="Pending", due_date=date(2026, 3, 4), today=today, urgent_window_days=7
        )
        == "pending"
    )


def test_compute_workflow_list_status_window_days_zero() -> None:
    today = date(2026, 2, 24)
    assert (
        compute_workflow_list_status(
            status="Pending", due_date=date(2026, 2, 24), today=today, urgent_window_days=0
        )
        == "urgent"
    )
    assert (
        compute_workflow_list_status(
            status="Pending", due_date=date(2026, 2, 25), today=today, urgent_window_days=0
        )
        == "pending"
    )


def test_compute_workflow_list_status_non_int_window_defaults() -> None:
    today = date(2026, 2, 24)
    assert (
        compute_workflow_list_status(
            status="Pending",
            due_date=date(2026, 3, 3),
            today=today,
            urgent_window_days="7",  # type: ignore[arg-type]
        )
        == "urgent"
    )
