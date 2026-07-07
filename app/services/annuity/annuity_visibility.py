from __future__ import annotations

from app.services.core.config_service import ConfigService

VISIBLE_CYCLE_COUNT_KEY = "ANNUITY_VISIBLE_CYCLE_COUNT"
VISIBLE_CYCLE_COUNT_DEFAULT = 2
VISIBLE_CYCLE_COUNT_MIN = 1
VISIBLE_CYCLE_COUNT_MAX = 5


def clamp_visible_cycle_count(
    value: object | None, *, default: int = VISIBLE_CYCLE_COUNT_DEFAULT
) -> int:
    """Clamp a candidate visible-cycle count to the supported range."""
    try:
        fallback = int(default)
    except Exception:
        fallback = VISIBLE_CYCLE_COUNT_DEFAULT
    fallback = max(VISIBLE_CYCLE_COUNT_MIN, min(fallback, VISIBLE_CYCLE_COUNT_MAX))

    try:
        if value is None:
            return fallback
        parsed = int(str(value).strip())
    except Exception:
        return fallback
    return max(VISIBLE_CYCLE_COUNT_MIN, min(parsed, VISIBLE_CYCLE_COUNT_MAX))


def get_visible_cycle_count(default: int = VISIBLE_CYCLE_COUNT_DEFAULT) -> int:
    """Read the configured visible-cycle window with a single canonical default."""
    configured = ConfigService.get_int(
        VISIBLE_CYCLE_COUNT_KEY,
        default,
        min_value=VISIBLE_CYCLE_COUNT_MIN,
        max_value=VISIBLE_CYCLE_COUNT_MAX,
    )
    return clamp_visible_cycle_count(configured, default=default)
