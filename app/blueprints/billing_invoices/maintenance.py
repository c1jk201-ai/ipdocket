"""
  

DB Restore Actions  Actions  User .
"""

import json
import os
from datetime import datetime, timezone
from typing import Dict, Optional

from flask import current_app

from app.utils.error_logging import report_swallowed_exception

_MAINT_CACHE = {
  "ts": 0.0,
  "enabled": False,
  "data": None,
}


def _atomic_write_json(path: str, data: Dict) -> None:
  """
  Write JSON atomically to avoid partially-written/corrupted files under concurrent access.
  Writes to a temp file in the same directory then replaces the target path.
  """
  directory = os.path.dirname(path) or "."
  os.makedirs(directory, exist_ok=True)
  tmp_path = f"{path}.tmp.{os.getpid()}"
  try:
    with open(tmp_path, "w", encoding="utf-8") as f:
      json.dump(data, f, ensure_ascii=False, indent=2)
      try:
        f.flush()
        os.fsync(f.fileno())
      except Exception as exc:
        # Best-effort durability: fsync may be unavailable on some filesystems.
        report_swallowed_exception(
          exc,
          context="billing_invoices.maintenance._atomic_write_json.fsync",
          log_key="billing_invoices.maintenance._atomic_write_json.fsync",
          log_window_seconds=300,
        )
    os.replace(tmp_path, path)
  finally:
    # Best-effort cleanup if something went wrong before replace()
    try:
      if os.path.exists(tmp_path):
        os.remove(tmp_path)
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.maintenance._atomic_write_json.cleanup_tmp",
        log_key="billing_invoices.maintenance._atomic_write_json.cleanup_tmp",
        log_window_seconds=300,
      )


def _read_maintenance_file() -> Optional[Dict]:
  try:
    path = _get_maintenance_file_path()
    if not os.path.exists(path):
      return None
    with open(path, "r", encoding="utf-8") as f:
      return json.load(f)
  except (OSError, json.JSONDecodeError, ValueError, TypeError):
    return None


def _get_cached_maintenance_data(ttl_seconds: float = 2.0) -> Optional[Dict]:
  try:
    now = datetime.now(timezone.utc).timestamp()
  except Exception:
    now = 0.0

  try:
    if _MAINT_CACHE.get("data") is not None and (
      now - float(_MAINT_CACHE.get("ts") or 0.0)
    ) <= float(ttl_seconds):
      return _MAINT_CACHE.get("data")
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.maintenance._get_cached_maintenance_data.cache_read",
      log_key="billing_invoices.maintenance._get_cached_maintenance_data.cache_read",
      log_window_seconds=300,
    )

  data = _read_maintenance_file()
  try:
    _MAINT_CACHE["ts"] = now
    _MAINT_CACHE["data"] = data
    _MAINT_CACHE["enabled"] = bool(data and data.get("enabled", False))
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.maintenance._get_cached_maintenance_data.cache_write",
      log_key="billing_invoices.maintenance._get_cached_maintenance_data.cache_write",
      log_window_seconds=300,
    )
  return data


def _get_maintenance_file_path() -> str:
  """ Status File """
  # Use SQLALCHEMY_DATABASE_URI or DB_PATH depending on what's available
  db_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
  if db_uri.startswith("sqlite:///"):
    db_path = db_uri.replace("sqlite:///", "")
  else:
    db_path = current_app.config.get("DB_PATH", "data/app.db")

  data_dir = os.path.dirname(db_path)
  os.makedirs(data_dir, exist_ok=True)
  return os.path.join(data_dir, ".maintenance_mode.json")


def is_maintenance_mode() -> bool:
  """Current  Confirm"""
  try:
    data = _get_cached_maintenance_data()
    return bool(data and data.get("enabled", False))
  except (OSError, json.JSONDecodeError, ValueError, TypeError):
    return False


def get_maintenance_info() -> Optional[Dict]:
  """  times"""
  try:
    data = _get_cached_maintenance_data()
    if not data:
      return None

    if not data.get("enabled", False):
      return None

    return {
      "enabled": data.get("enabled", False),
      "reason": data.get("reason", " "),
      "started_at": data.get("started_at"),
      "started_by": data.get("started_by"),
      "estimated_end": data.get("estimated_end"),
    }
  except (OSError, json.JSONDecodeError, ValueError, TypeError):
    return None


def enable_maintenance_mode(
  reason: str = "DB Restore Actions ",
  started_by: Optional[str] = None,
  estimated_duration_seconds: int = 60,
) -> bool:
  """
   active

  Args:
    reason: 
    started_by: Actions (Userpeople)
    estimated_duration_seconds: Estimated  ()

  Returns:
    success 
  """
  try:
    path = _get_maintenance_file_path()
    now = datetime.now(timezone.utc)

    data = {
      "enabled": True,
      "reason": reason,
      "started_at": now.isoformat(),
      "started_by": started_by,
      "estimated_end": (now.timestamp() + estimated_duration_seconds),
    }

    _atomic_write_json(path, data)

    try:
      _MAINT_CACHE["ts"] = now.timestamp()
      _MAINT_CACHE["data"] = data
      _MAINT_CACHE["enabled"] = True
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.maintenance.enable_maintenance_mode.cache_write",
        log_key="billing_invoices.maintenance.enable_maintenance_mode.cache_write",
        log_window_seconds=300,
      )

    current_app.logger.info(f" active: {reason} (: {started_by})")
    return True
  except (OSError, ValueError, TypeError) as e:
    current_app.logger.error(f" active : {e}")
    return False


def disable_maintenance_mode() -> bool:
  """
   disabled

  Returns:
    success 
  """
  try:
    path = _get_maintenance_file_path()

    # File Delete
    if os.path.exists(path):
      os.remove(path)

    try:
      _MAINT_CACHE["ts"] = datetime.now(timezone.utc).timestamp()
      _MAINT_CACHE["data"] = None
      _MAINT_CACHE["enabled"] = False
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.maintenance.disable_maintenance_mode.cache_write",
        log_key="billing_invoices.maintenance.disable_maintenance_mode.cache_write",
        log_window_seconds=300,
      )

    current_app.logger.info(" disabled")
    return True
  except OSError as e:
    current_app.logger.error(f" disabled : {e}")
    return False


def force_disable_maintenance_mode() -> bool:
  """
    disabled (  )

  Returns:
    success 
  """
  try:
    path = _get_maintenance_file_path()

    if os.path.exists(path):
      os.remove(path)
      try:
        _MAINT_CACHE["ts"] = datetime.now(timezone.utc).timestamp()
        _MAINT_CACHE["data"] = None
        _MAINT_CACHE["enabled"] = False
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context="billing_invoices.maintenance.force_disable_maintenance_mode.cache_write",
          log_key="billing_invoices.maintenance.force_disable_maintenance_mode.cache_write",
          log_window_seconds=300,
        )
      return True
    return False
  except OSError:
    return False


def is_maintenance_stale(max_age_seconds: int = 3600) -> bool:
  """
    Confirm (Auto )

  Args:
    max_age_seconds:  (Default 1)

  Returns:
     Status 
  """
  try:
    info = get_maintenance_info()
    if not info:
      return False

    started_at_str = info.get("started_at")
    if not started_at_str:
      return False

    started_at = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)

    age_seconds = (now - started_at).total_seconds()
    return age_seconds > max_age_seconds
  except (ValueError, TypeError):
    return False
