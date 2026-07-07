from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.exc import DBAPIError

from app.services.billing.db_core import DB_ERRORS, DatabaseError, get_db


def get_fx_rates_cache(max_age_seconds: int = 3600, source: str = "sample"):
  """Return cached FX payload (as dict) if not older than max_age_seconds; else None."""
  conn = None
  try:
    conn = get_db()
    row = conn.execute(
      "SELECT payload, fetched_at FROM fx_rates_cache WHERE source=?",
      (source,),
    ).fetchone()
    if not row:
      return None
    payload_str = row["payload"] if hasattr(row, "keys") else row[0]
    fetched_at_str = row["fetched_at"] if hasattr(row, "keys") else row[1]
    if not payload_str or not fetched_at_str:
      return None
    try:
      fetched_dt = datetime.fromisoformat(fetched_at_str)
    except ValueError:
      # Fallback: treat as ET naive
      fetched_dt = datetime.now(ZoneInfo("America/New_York"))
    now_kst = datetime.now(ZoneInfo("America/New_York"))
    age = (
      now_kst
      - (
        fetched_dt
        if fetched_dt.tzinfo
        else fetched_dt.replace(tzinfo=ZoneInfo("America/New_York"))
      )
    ).total_seconds()
    if age <= max_age_seconds:
      return json.loads(payload_str)
    return None
  except (
    DBAPIError,
    DatabaseError,
    json.JSONDecodeError,
    TypeError,
    ValueError,
    KeyError,
    OSError,
  ):
    return None
  finally:
    try:
      if conn:
        conn.close()
    except DB_ERRORS:
      pass


def set_fx_rates_cache(payload: dict, source: str = "sample") -> None:
  """Upsert FX rates payload with current ET timestamp."""
  conn = None
  try:
    conn = get_db()
    now_kst = datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds")
    conn.execute(
      """
      INSERT INTO fx_rates_cache (source, payload, fetched_at)
      VALUES (?, ?, ?)
      ON CONFLICT(source) DO UPDATE SET
       payload=excluded.payload,
       fetched_at=excluded.fetched_at
      """,
      (source, json.dumps(payload, ensure_ascii=False), now_kst),
    )
    conn.commit()
  except (DBAPIError, DatabaseError, TypeError, ValueError, KeyError, OSError):
    pass
  finally:
    try:
      if conn:
        conn.close()
    except DB_ERRORS:
      pass
