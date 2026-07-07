from __future__ import annotations

from datetime import date, timedelta

COMPLETED_WORKFLOW_STATUS_KEYS = frozenset({"completed", "complete", "done"})
ABANDONED_WORKFLOW_STATUS_KEYS = frozenset({"abandoned", "cancelled", "canceled"})
TERMINAL_WORKFLOW_STATUS_KEYS = COMPLETED_WORKFLOW_STATUS_KEYS | ABANDONED_WORKFLOW_STATUS_KEYS


def normalize_workflow_status_key(status: str | None) -> str:
    return (status or "").strip().lower().replace("_", " ").replace("-", " ")


def is_workflow_terminal_status(status: str | None) -> bool:
    return normalize_workflow_status_key(status) in TERMINAL_WORKFLOW_STATUS_KEYS


def compute_workflow_list_status(
    *,
    status: str | None,
    due_date: date | None,
    today: date,
    urgent_window_days: int = 7,
) -> str:
    """
    Compute a UI-friendly workflow list status.

    Returns one of:
      - "completed"
      - "abandoned"
      - "overdue"
      - "urgent"
      - "pending"
    """

    raw = normalize_workflow_status_key(status)
    if raw in COMPLETED_WORKFLOW_STATUS_KEYS:
        return "completed"
    if raw in ABANDONED_WORKFLOW_STATUS_KEYS:
        return "abandoned"

    try:
        window = int(urgent_window_days)
    except Exception:
        window = 7
    window = max(0, min(window, 3650))

    urgent_date = today + timedelta(days=window)

    if due_date and due_date < today:
        return "overdue"
    if due_date and due_date <= urgent_date:
        return "urgent"
    return "pending"
