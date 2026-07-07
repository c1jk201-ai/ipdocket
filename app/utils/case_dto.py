from __future__ import annotations

from typing import Any, Dict, Optional

from app.models.ip_records import Matter


def matter_summary(matter: Matter) -> Dict[str, Optional[Any]]:
    """Serialize Matter into a UI-friendly dict.

    Normalizes naming so templates/JS do not rely on model attribute names
    (e.g., `right_name` vs `title`).
    """

    if not matter:
        return {}

    return {
        "id": getattr(matter, "matter_id", None),
        "our_ref": getattr(matter, "our_ref", None),
        # Normalize title -> use right_name fallback to memo/empty
        "title": getattr(matter, "right_name", None) or getattr(matter, "memo", None),
        "division": getattr(matter, "right_group", None),
        "matter_type": getattr(matter, "matter_type", None),
        "status_red": getattr(matter, "status_red", None),
        "status_blue": getattr(matter, "status_blue", None),
    }
