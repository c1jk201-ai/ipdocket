from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


_SETTLEMENT_PERCENT_TOLERANCE = Decimal("0.01")


def _int_or_none(value: Any) -> int | None:
  try:
    return int(value)
  except Exception:
    return None


def _decimal_or_none(value: Any) -> Decimal | None:
  try:
    return Decimal(str(value))
  except (InvalidOperation, ValueError, TypeError):
    return None


def is_default_settlement_split(
  splits: Any,
  issuing_business_profile_id: Any,
) -> bool:
  """Return True when settlement split data is only the implicit default.

  The invoice system already treats the issuing business profile as owning
  100% settlement when no split metadata exists. A stored single-row 100%
  split to that same profile is therefore just UI noise, not an internal
  settlement requirement.
  """
  issuing_bp_id = _int_or_none(issuing_business_profile_id)
  if not issuing_bp_id or not isinstance(splits, list):
    return False

  active_splits: list[tuple[int, Decimal]] = []
  for record in splits:
    if not isinstance(record, dict):
      continue
    bp_id = _int_or_none(record.get("business_profile_id"))
    percent = _decimal_or_none(record.get("percent"))
    if not bp_id or percent is None or percent <= 0:
      continue
    active_splits.append((bp_id, percent))

  if len(active_splits) != 1:
    return False

  bp_id, percent = active_splits[0]
  return (
    bp_id == issuing_bp_id
    and abs(percent - Decimal("100")) <= _SETTLEMENT_PERCENT_TOLERANCE
  )
