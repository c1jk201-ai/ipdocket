from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import current_app

from app.extensions import db
from app.models.job_run import JobRun


def _kst_tz() -> ZoneInfo:
  try:
    tz_name = current_app.config.get("TIMEZONE", "America/New_York")
  except Exception:
    tz_name = "America/New_York"
  try:
    return ZoneInfo(tz_name or "America/New_York")
  except Exception:
    return ZoneInfo("America/New_York")


def _to_kst(dt: datetime | None) -> datetime | None:
  if dt is None:
    return None
  tz = _kst_tz()
  if dt.tzinfo is None:
    # JobRun timestamps are stored as UTC naive.
    dt = dt.replace(tzinfo=timezone.utc)
  try:
    return dt.astimezone(tz)
  except Exception:
    return dt


def _fmt_kst(dt: datetime | None) -> str | None:
  converted = _to_kst(dt)
  if converted is None:
    return None
  return converted.strftime("%Y-%m-%d %H:%M:%S")


def _scheduler_running_info() -> tuple[bool, bool, datetime | None]:
  local_running = current_app.extensions.get("apscheduler") is not None

  try:
    hb_interval = int(
      current_app.config.get("SCHEDULER_HEARTBEAT_INTERVAL_SECONDS", 300) or 300
    )
  except Exception:
    hb_interval = 300
  try:
    hb_grace = int(current_app.config.get("SCHEDULER_HEARTBEAT_GRACE_SECONDS", 600) or 600)
  except Exception:
    hb_grace = 600
  lookback_seconds = max(120, hb_grace, hb_interval * 3)
  lookback = datetime.utcnow() - timedelta(seconds=lookback_seconds)

  last_hb = (
    db.session.query(JobRun.finished_at)
    .filter(
      JobRun.job_name == "scheduler_heartbeat",
      JobRun.status == "success",
      JobRun.finished_at >= lookback,
    )
    .order_by(JobRun.finished_at.desc())
    .limit(1)
    .scalar()
  )
  heartbeat_ok = bool(last_hb)
  running = bool(local_running or heartbeat_ok)
  return running, local_running, last_hb


def get_scheduler_status() -> dict:
  running, local_running, last_hb = _scheduler_running_info()
  return {
    "running": running,
    "localRunning": local_running,
    "heartbeatLastSuccess": _fmt_kst(last_hb),
  }
