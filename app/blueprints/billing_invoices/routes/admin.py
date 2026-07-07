"""
Administrator  Blueprint

User ,  .
PostgreSQL Version - Backup pg_dump 
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import uuid
import zipfile
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import (
  Blueprint,
  current_app,
  flash,
  g,
  redirect,
  render_template,
  request,
  send_file,
  url_for,
)
from sqlalchemy import text

from app.extensions import db as core_db
from app.models.job_run import JobRun
from app.utils.error_logging import report_swallowed_exception

from ..auth import get_current_user, log_audit, role_required
from ..db import get_db


def _display_datetime(value: datetime | None) -> str | None:
  if not value:
    return None
  tzname = current_app.config.get("TIMEZONE", "America/New_York")
  if value.tzinfo is None:
    value = value.replace(tzinfo=ZoneInfo("UTC"))
  local = value.astimezone(ZoneInfo(tzname))
  return local.strftime(current_app.config.get("DATETIME_FORMAT", "%m/%d/%Y %I:%M:%S %p"))


bp = Blueprint("admin", __name__)


_BACKUP_NAME_RE = re.compile(r"^(backup|pre-restore)-\d{14}\.(db|sql|dump)$")
_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ADMIN_JOB_NAMES = {
  "backup.create",
  "backup.create_full",
  "backup.restore",
  "backup.restore_file",
}
_ADMIN_RESTORE_JOB_NAMES = {"backup.restore", "backup.restore_file"}
_ADMIN_JOB_RUNNING_STATUSES = {"queued", "running"}


def _log_audit_for_user(
  *,
  actor_id: int | None,
  action: str,
  target_type: str | None = None,
  target_id: int | None = None,
  meta: dict | str | None = None,
  request_id: str | None = None,
) -> None:
  if not actor_id:
    return
  payload = None
  if isinstance(meta, str):
    payload = meta
  elif meta is not None:
    try:
      payload = json.dumps(meta, ensure_ascii=False)
    except Exception:
      payload = str(meta)
  conn = get_db()
  try:
    conn.execute(
      "INSERT INTO audit_log (request_id, actor_id, user_id, action, target_type, target_id, meta) "
      "VALUES (?, ?, ?, ?, ?, ?, ?)",
      (request_id, actor_id, actor_id, action, target_type, target_id, payload),
    )
  except Exception:
    conn.execute(
      "INSERT INTO audit_log (user_id, action, target_type, target_id, meta) VALUES (?, ?, ?, ?, ?)",
      (actor_id, action, target_type, target_id, payload),
    )
  conn.commit()
  conn.close()


def _admin_job_stale_minutes() -> int:
  try:
    return int(current_app.config.get("ADMIN_JOB_STALE_MINUTES", 120) or 120)
  except Exception:
    return 120


def _is_job_stale(job_run: JobRun, *, stale_minutes: int) -> bool:
  if not job_run or not job_run.started_at:
    return False
  age = datetime.utcnow() - job_run.started_at
  return age > timedelta(minutes=stale_minutes)


def _mark_job_stale(job_run: JobRun, *, reason: str) -> None:
  if not job_run:
    return
  job_run.status = "failed"
  job_run.error = reason
  job_run.finished_at = datetime.utcnow()
  try:
    core_db.session.commit()
  except Exception:
    core_db.session.rollback()


def _find_active_job(job_names: set[str]):
  try:
    return (
      JobRun.query.filter(JobRun.job_name.in_(list(job_names)))
      .filter(JobRun.status.in_(list(_ADMIN_JOB_RUNNING_STATUSES)))
      .order_by(JobRun.started_at.desc())
      .first()
    )
  except Exception:
    return None


def _assert_no_active_restore_job() -> None:
  active = _find_active_job(_ADMIN_RESTORE_JOB_NAMES)
  if not active:
    return
  if _is_job_stale(active, stale_minutes=_admin_job_stale_minutes()):
    _mark_job_stale(active, reason="stale admin restore job")
    return
  raise RuntimeError(
    f"Restore already in progress (run_id={active.run_id}, status={active.status})."
  )


def _reset_core_db_session() -> None:
  try:
    core_db.session.remove()
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.admin._reset_core_db_session.session_remove",
      log_key="billing_invoices.admin._reset_core_db_session.session_remove",
      log_window_seconds=300,
    )
  try:
    core_db.engine.dispose()
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.admin._reset_core_db_session.engine_dispose",
      log_key="billing_invoices.admin._reset_core_db_session.engine_dispose",
      log_window_seconds=300,
    )


def _get_job_run(run_id: str) -> JobRun | None:
  try:
    return JobRun.query.filter_by(run_id=run_id).first()
  except Exception:
    return None


def _enqueue_admin_job(job_name: str, payload: dict) -> str:
  if job_name not in _ADMIN_JOB_NAMES:
    raise ValueError("Unknown admin job name.")
  if job_name in _ADMIN_RESTORE_JOB_NAMES:
    _assert_no_active_restore_job()
  run_id = uuid.uuid4().hex
  job_run = JobRun(
    job_name=job_name,
    run_id=run_id,
    status="queued",
    started_at=datetime.utcnow(),
    request_id=str(getattr(g, "request_id", "") or "") or run_id,
  )
  try:
    job_run.input_ref = json.dumps(payload, ensure_ascii=False)
  except Exception:
    job_run.input_ref = str(payload)
  core_db.session.add(job_run)
  core_db.session.commit()
  return run_id


def _admin_job_payload(user: dict | None) -> dict:
  request_id = getattr(g, "request_id", None)
  actor_id = user.get("id") if user else None
  actor_name = None
  if user:
    actor_name = user.get("username") or user.get("display_name")
  return {
    "actor_id": actor_id,
    "actor_name": actor_name or "admin",
    "request_id": str(request_id) if request_id else None,
  }


def _restore_postgres_from_backup(path: str) -> str:
  pg_info = _get_pg_connection_info()
  if not pg_info:
    raise RuntimeError("PostgreSQL Link not found.")

  ext = os.path.splitext(path)[1].lower()
  if ext not in (".sql", ".dump"):
    raise RuntimeError("PostgreSQL .sql .dump Backup Restore .")

  try:
    base_real = os.path.realpath(_backup_dir())
    path_real = os.path.realpath(path)
    if os.path.commonpath([base_real, path_real]) != base_real:
      raise RuntimeError(" Backup .")
  except Exception:
    raise RuntimeError(" Backup .")

  env = os.environ.copy()
  env["PGPASSWORD"] = pg_info["password"]
  log_path = os.path.join(
    _backup_dir(), f"restore-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.log"
  )

  if ext == ".sql":
    if shutil.which("psql"):
      cmd = [
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-1",
        "-h",
        pg_info["host"],
        "-p",
        pg_info["port"],
        "-U",
        pg_info["username"],
        "-d",
        pg_info["database"],
        "-f",
        path,
      ]
      result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
      log_cmd = "psql"
    elif shutil.which("docker"):
      container_name = _safe_container_name(os.environ.get("DB_CONTAINER_NAME", "new_IPM-db"))
      cmd = [
        "docker",
        "exec",
        "-i",
        "-e",
        f"PGPASSWORD={pg_info['password']}",
        container_name,
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-1",
        "-U",
        pg_info["username"],
        "-d",
        pg_info["database"],
      ]
      with open(path, "r", encoding="utf-8") as f:
        result = subprocess.run(cmd, stdin=f, capture_output=True, text=True, timeout=600)
      log_cmd = "psql (docker)"
    else:
      raise RuntimeError("psql docker not found.")
  else:
    # Custom dump format (.dump) restore
    try:
      no_owner = bool(current_app.config.get("BACKUP_PG_NO_OWNER", True))
    except Exception:
      no_owner = True
    try:
      no_privs = bool(current_app.config.get("BACKUP_PG_NO_PRIVILEGES", True))
    except Exception:
      no_privs = True

    restore_flags = ["--clean", "--if-exists", "--exit-on-error", "--single-transaction"]
    if no_owner:
      restore_flags.append("--no-owner")
    if no_privs:
      restore_flags.append("--no-privileges")

    if shutil.which("pg_restore"):
      cmd = [
        "pg_restore",
        "-h",
        pg_info["host"],
        "-p",
        pg_info["port"],
        "-U",
        pg_info["username"],
        "-d",
        pg_info["database"],
        *restore_flags,
        path,
      ]
      result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
      log_cmd = "pg_restore"
    elif shutil.which("docker"):
      container_name = _safe_container_name(os.environ.get("DB_CONTAINER_NAME", "new_IPM-db"))
      cmd = [
        "docker",
        "exec",
        "-i",
        "-e",
        f"PGPASSWORD={pg_info['password']}",
        container_name,
        "pg_restore",
        "-U",
        pg_info["username"],
        "-d",
        pg_info["database"],
        *restore_flags,
        "-",
      ]
      with open(path, "rb") as f:
        result = subprocess.run(cmd, stdin=f, capture_output=True, text=True, timeout=600)
      log_cmd = "pg_restore (docker)"
    else:
      raise RuntimeError("pg_restore docker not found.")

  _write_job_log(
    log_path,
    stdout=result.stdout,
    stderr=result.stderr,
    header={
      "command": log_cmd,
      "backup": os.path.basename(path),
    },
  )

  if result.returncode != 0:
    raise RuntimeError(f"restore failed: {result.stderr}")

  return log_path


def _run_admin_job(job_name: str, payload: dict) -> dict:
  actor_id = payload.get("actor_id")
  actor_name = payload.get("actor_name")
  request_id = payload.get("request_id")

  if job_name == "backup.create":
    backup_path = _create_backup_file()
    log_path = _backup_log_path(backup_path)
    _log_audit_for_user(
      actor_id=actor_id,
      action="backup.create",
      target_type="backup",
      meta={"path": backup_path},
      request_id=request_id,
    )
    try:
      _write_backup_meta(
        backup_path,
        source="manual",
        created_by=actor_id,
        trigger="admin",
        request_id=request_id,
        log_path=log_path,
      )
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.admin.backup_create.write_backup_meta",
        log_key="billing_invoices.admin.backup_create.write_backup_meta",
        log_window_seconds=300,
      )
    try:
      _cleanup_old_backups()
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.admin.backup_create.cleanup_old_backups",
        log_key="billing_invoices.admin.backup_create.cleanup_old_backups",
        log_window_seconds=300,
      )
    return {"backup_path": backup_path, "log_path": log_path}

  if job_name == "backup.create_full":
    backup_path = _create_backup_file()
    log_path = _backup_log_path(backup_path)
    name = os.path.basename(backup_path)
    m = re.match(r"^backup-(\d{14})\.(db|sql|dump)$", name)
    ts = m.group(1) if m else datetime.utcnow().strftime("%Y%m%d%H%M%S")
    zip_path = _zip_attachments(ts)
    _log_audit_for_user(
      actor_id=actor_id,
      action="backup.create_full",
      target_type="backup",
      meta={"db": backup_path, "attachments_zip": zip_path, "includes_uploads": True},
      request_id=request_id,
    )
    try:
      _write_backup_meta(
        backup_path,
        source="manual",
        note="DB+attachments+uploads",
        tags=["with_attachments", "with_uploads"],
        created_by=actor_id,
        trigger="admin",
        request_id=request_id,
        log_path=log_path,
      )
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.admin.backup_create_full.write_backup_meta_db",
        log_key="billing_invoices.admin.backup_create_full.write_backup_meta_db",
        log_window_seconds=300,
      )
    try:
      _write_backup_meta(
        zip_path,
        source="manual",
        note="attachments+uploads",
        tags=["attachments", "uploads"],
        created_by=actor_id,
        trigger="admin",
        request_id=request_id,
      )
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.admin.backup_create_full.write_backup_meta_attachments",
        log_key="billing_invoices.admin.backup_create_full.write_backup_meta_attachments",
        log_window_seconds=300,
      )
    try:
      _cleanup_old_backups()
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.admin.backup_create_full.cleanup_old_backups",
        log_key="billing_invoices.admin.backup_create_full.cleanup_old_backups",
        log_window_seconds=300,
      )
    return {
      "backup_path": backup_path,
      "attachments_zip": zip_path,
      "includes_uploads": True,
      "log_path": log_path,
    }

  if job_name in ("backup.restore", "backup.restore_file"):
    from ..maintenance import disable_maintenance_mode, enable_maintenance_mode

    restore_path = payload.get("backup_path")
    if not restore_path:
      raise RuntimeError("Restore target Backup File not available.")

    reason = "DB Restore Actions "
    if payload.get("restore_at"):
      reason = f"DB Restore Actions (Restore : {payload.get('restore_at')})"
    elif payload.get("backup_name"):
      reason = f"DB Restore Actions (Restore File: {payload.get('backup_name')})"

    enable_maintenance_mode(
      reason=reason,
      started_by=actor_name,
      estimated_duration_seconds=60,
    )
    try:
      try:
        _create_backup_file()
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context="billing_invoices.admin.backup_restore.pre_restore_backup",
          log_key="billing_invoices.admin.backup_restore.pre_restore_backup",
          log_window_seconds=300,
        )
      restore_log_path = _restore_postgres_from_backup(restore_path)
      action = (
        "backup.restore_file" if job_name == "backup.restore_file" else "backup.restore"
      )
      _log_audit_for_user(
        actor_id=actor_id,
        action=action,
        target_type="backup",
        meta={
          "from": os.path.basename(restore_path),
          "target_at": payload.get("restore_at"),
        },
        request_id=request_id,
      )
      return {"restored_from": restore_path, "log_path": restore_log_path}
    finally:
      disable_maintenance_mode()

  raise RuntimeError(f"Unknown admin job: {job_name}")


def _run_admin_job_background(run_id: str, job_name: str, payload: dict) -> None:
  app = current_app._get_current_object()

  def _runner():
    with app.app_context():
      job_run = _get_job_run(run_id)
      if job_run:
        job_run.status = "running"
        job_run.started_at = datetime.utcnow()
        try:
          core_db.session.commit()
        except Exception:
          core_db.session.rollback()

      try:
        result = _run_admin_job(job_name, payload)
        if job_name in _ADMIN_RESTORE_JOB_NAMES:
          _reset_core_db_session()
        job_run = _get_job_run(run_id)
        if job_run:
          job_run.status = "success"
          try:
            job_run.output_ref = json.dumps(result, ensure_ascii=False)
          except Exception:
            job_run.output_ref = str(result)
      except Exception as exc:
        current_app.logger.exception("Admin job failed: %s", job_name)
        if job_name in _ADMIN_RESTORE_JOB_NAMES:
          _reset_core_db_session()
        job_run = _get_job_run(run_id)
        if job_run:
          job_run.status = "failed"
          job_run.error = str(exc)
      finally:
        if job_run:
          job_run.finished_at = datetime.utcnow()
          try:
            core_db.session.commit()
          except Exception:
            core_db.session.rollback()
        _reset_core_db_session()

  thread = threading.Thread(target=_runner, daemon=True)
  thread.start()


def _resolve_backup_path(name: str):
  name = (name or "").strip()
  if not _BACKUP_NAME_RE.match(name):
    return None
  base_dir = _backup_dir()
  path = os.path.join(base_dir, name)
  try:
    base_real = os.path.realpath(base_dir)
    path_real = os.path.realpath(path)
    if not path_real.startswith(base_real + os.sep):
      return None
  except Exception:
    return None
  return path


def _backup_dir() -> str:
  d = current_app.config.get("BACKUP_DIR")
  os.makedirs(d, exist_ok=True)
  return d


def _safe_container_name(raw: str | None) -> str:
  name = (raw or "").strip()
  if not name:
    raise RuntimeError("DB_CONTAINER_NAME is empty.")
  if not _CONTAINER_NAME_RE.match(name):
    raise RuntimeError("DB_CONTAINER_NAME is invalid.")
  return name


def _backup_log_path(backup_path: str) -> str:
  base, _ext = os.path.splitext(backup_path)
  return base + ".log"


def _write_job_log(
  log_path: str,
  *,
  stdout: str | None = None,
  stderr: str | None = None,
  header: dict | None = None,
) -> None:
  try:
    with open(log_path, "w", encoding="utf-8") as f:
      if header:
        for key, value in header.items():
          f.write(f"{key}: {value}\n")
        f.write("\n")
      f.write("=== STDOUT ===\n")
      if stdout:
        f.write(str(stdout))
      f.write("\n=== STDERR ===\n")
      if stderr:
        f.write(str(stderr))
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.admin._write_job_log",
      log_key="billing_invoices.admin._write_job_log",
      log_window_seconds=300,
    )


def _get_pg_connection_info():
  """Parse PostgreSQL connection info from DATABASE_URL."""
  db_url = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
  if not db_url.startswith("postgresql"):
    return None

  parsed = urlparse(db_url)
  return {
    "host": parsed.hostname or "localhost",
    "port": str(parsed.port) if parsed.port else "5432",
    "database": parsed.path.lstrip("/") if parsed.path else "",
    "username": parsed.username or "",
    "password": parsed.password or "",
  }


def _cleanup_old_backups():
  """Delete old backup artifacts using retention settings.
  Skips pre-restore DB backups.
  Also skips artifacts referenced by active backup_sets retention rows.
  Manual DB backups (sidecar source='manual') are skipped for age/count-based cleanup,
  but can still be removed by the optional byte-cap cleanup (BACKUP_RETENTION_MAX_BYTES)
  unless tagged with "keep".
  Supports patterns:
   - backup-YYYYmmddHHMMSS.db
   - backup-YYYYmmddHHMMSS.sql
   - backup-YYYYmmddHHMMSS.dump
   - pre-restore-YYYYmmddHHMMSS.db
   - attachments-YYYYmmddHHMMSS.zip
  """
  backup_dir = _backup_dir()
  backup_exts = (".db", ".sql", ".dump")
  retention = int(current_app.config.get("BACKUP_RETENTION_DAYS", 7) or 0)
  max_count = int(current_app.config.get("BACKUP_RETENTION_MAX_COUNT", 0) or 0)
  max_bytes = int(current_app.config.get("BACKUP_RETENTION_MAX_BYTES", 0) or 0)
  attachments_retention = int(
    current_app.config.get("BACKUP_ATTACHMENTS_RETENTION_DAYS", retention) or 0
  )
  attachments_max_count = int(
    current_app.config.get("BACKUP_ATTACHMENTS_RETENTION_MAX_COUNT", 0) or 0
  )
  attachments_max_bytes = int(
    current_app.config.get("BACKUP_ATTACHMENTS_RETENTION_MAX_BYTES", 0) or 0
  )
  transfer_retention = int(
    current_app.config.get("TRANSFER_BUNDLE_RETENTION_DAYS", retention) or 0
  )
  transfer_max_count = int(current_app.config.get("TRANSFER_BUNDLE_RETENTION_MAX_COUNT", 0) or 0)
  transfer_max_bytes = int(current_app.config.get("TRANSFER_BUNDLE_RETENTION_MAX_BYTES", 0) or 0)
  cutoff = datetime.utcnow() - timedelta(days=retention) if retention > 0 else None
  cutoff_attachments = (
    datetime.utcnow() - timedelta(days=attachments_retention)
    if attachments_retention > 0
    else None
  )
  cutoff_transfer = (
    datetime.utcnow() - timedelta(days=transfer_retention) if transfer_retention > 0 else None
  )

  def _canonical_path(path: str | None) -> str | None:
    if not path:
      return None
    try:
      return os.path.realpath(str(path))
    except Exception:
      return None

  def _iter_artifact_paths(payload):
    if isinstance(payload, str):
      yield payload
      return
    if isinstance(payload, dict):
      for value in payload.values():
        yield from _iter_artifact_paths(value)
      return
    if isinstance(payload, (list, tuple, set)):
      for value in payload:
        yield from _iter_artifact_paths(value)

  def _load_retention_protected_paths() -> set[str]:
    protected: set[str] = set()
    now_utc = datetime.utcnow()
    try:
      rows = core_db.session.execute(
        text(
          """
          SELECT artifact_paths_json
          FROM backup_sets
          WHERE artifact_paths_json IS NOT NULL
           AND (retention_until IS NULL OR retention_until > :now_utc)
          """
        ),
        {"now_utc": now_utc},
      ).all()
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.admin._cleanup_old_backups.load_retention_protected_paths",
        log_key="billing_invoices.admin._cleanup_old_backups.load_retention_protected_paths",
        log_window_seconds=300,
      )
      return protected

    for row in rows:
      artifact_payload = row[0] if row else None
      for raw_path in _iter_artifact_paths(artifact_payload):
        canonical = _canonical_path(raw_path)
        if canonical:
          protected.add(canonical)
    return protected

  retention_protected_paths = _load_retention_protected_paths()

  def _is_retention_protected(path: str) -> bool:
    canonical = _canonical_path(path)
    return bool(canonical and canonical in retention_protected_paths)

  def _parse_ts(ts_str: str) -> datetime | None:
    try:
      return datetime.strptime(ts_str, "%Y%m%d%H%M%S")
    except Exception:
      return None

  def _tags_from_meta(meta: dict) -> set[str]:
    try:
      return {str(t).strip().lower() for t in (meta.get("tags") or []) if str(t).strip()}
    except Exception:
      return set()

  def _source_from_meta(meta: dict) -> str:
    try:
      return (meta.get("source") or "unknown").strip().lower()
    except Exception:
      return "unknown"

  def _read_meta(path: str) -> dict:
    """Read metadata sidecar for a backup artifact (best-effort)."""
    meta = {}
    try:
      meta = _read_backup_meta(path)
    except Exception:
      meta = {}
    if isinstance(meta, dict) and meta:
      return meta
    # Transfer bundles use a full-suffix sidecar: *.tar.gz.json
    alt_json = f"{path}.json"
    if os.path.exists(alt_json):
      try:
        with open(alt_json, encoding="utf-8") as f:
          data = json.load(f)
        if isinstance(data, dict):
          return data
      except Exception:
        return {}
    return {}

  def _remove_with_sidecars(path: str) -> None:
    if os.path.exists(path):
      os.remove(path)
    base, _ext = os.path.splitext(path)
    for extra in (base + ".json", base + ".log"):
      if os.path.exists(extra):
        try:
          os.remove(extra)
        except Exception as exc:
          report_swallowed_exception(
            exc,
            context="billing_invoices.admin._cleanup_old_backups.remove_sidecar",
            log_key="billing_invoices.admin._cleanup_old_backups.remove_sidecar",
            log_window_seconds=300,
          )

  def _remove_transfer_with_sidecars(path: str) -> None:
    if os.path.exists(path):
      os.remove(path)
    for extra in (f"{path}.sha256", f"{path}.json", f"{path}.log"):
      if os.path.exists(extra):
        try:
          os.remove(extra)
        except Exception as exc:
          report_swallowed_exception(
            exc,
            context="billing_invoices.admin._cleanup_old_backups.remove_transfer_sidecar",
            log_key="billing_invoices.admin._cleanup_old_backups.remove_transfer_sidecar",
            log_window_seconds=300,
          )

  def _parse_transfer_ts(name: str) -> datetime | None:
    m = re.search(r"(\d{14})(?=\.tar\.gz(?:\.|$))", name)
    if not m:
      return None
    return _parse_ts(m.group(1))

  backup_items: list[tuple[datetime, str, dict]] = []
  attachment_items: list[tuple[datetime, str, dict]] = []
  transfer_items: list[tuple[datetime, str, dict]] = []
  for root, _dirs, files in os.walk(backup_dir):
    for name in files:
      path = os.path.join(root, name)
      if not os.path.isfile(path):
        continue
      rel = os.path.relpath(path, backup_dir).replace("\\", "/")
      is_transfer_artifact = rel.startswith("transfer_bundles/")

      if is_transfer_artifact and name.endswith(".tar.gz"):
        meta = _read_meta(path)
        tags = _tags_from_meta(meta)
        dt = _parse_transfer_ts(name)
        if not dt:
          try:
            dt = datetime.utcfromtimestamp(float(os.path.getmtime(path)))
          except Exception:
            dt = datetime.utcnow()
        transfer_items.append((dt, path, meta if isinstance(meta, dict) else {}))
        if (
          cutoff_transfer
          and dt < cutoff_transfer
          and "keep" not in tags
          and not _is_retention_protected(path)
        ):
          try:
            _remove_transfer_with_sidecars(path)
          except Exception as exc:
            report_swallowed_exception(
              exc,
              context="billing_invoices.admin._cleanup_old_backups.remove_transfer_bundle",
              log_key="billing_invoices.admin._cleanup_old_backups.remove_transfer_bundle",
              log_window_seconds=300,
            )
        continue

      if name.startswith("restore-") and name.endswith(".log"):
        if not cutoff:
          continue
        ts_str = name[len("restore-") : -len(".log")]
        dt = _parse_ts(ts_str)
        if not dt:
          continue
        if dt < cutoff:
          try:
            os.remove(path)
          except Exception as exc:
            report_swallowed_exception(
              exc,
              context="billing_invoices.admin._cleanup_old_backups.remove_restore_log",
              log_key="billing_invoices.admin._cleanup_old_backups.remove_restore_log",
              log_window_seconds=300,
            )
        continue
      if name.startswith("attachments-") and name.endswith(".zip"):
        ts_str = name[len("attachments-") : -len(".zip")]
        meta = _read_meta(path)
        tags = _tags_from_meta(meta)
        dt = _parse_ts(ts_str)
        if dt:
          attachment_items.append((dt, path, meta if isinstance(meta, dict) else {}))
        if (
          cutoff_attachments
          and dt
          and dt < cutoff_attachments
          and "keep" not in tags
          and not _is_retention_protected(path)
        ):
          try:
            _remove_with_sidecars(path)
          except Exception as exc:
            report_swallowed_exception(
              exc,
              context="billing_invoices.admin._cleanup_old_backups.remove_attachments_zip",
              log_key="billing_invoices.admin._cleanup_old_backups.remove_attachments_zip",
              log_window_seconds=300,
            )
        continue
      ext = None
      for candidate in backup_exts:
        if name.endswith(candidate):
          ext = candidate
          break
      if not ext:
        continue
      # Never delete pre-restore backups automatically
      if name.startswith("pre-restore-"):
        continue
      if name.startswith("backup-"):
        ts_str = name[len("backup-") : -len(ext)]
      else:
        continue
      dt = _parse_ts(ts_str)
      if not dt:
        continue
      meta = _read_meta(path)
      backup_items.append((dt, path, meta if isinstance(meta, dict) else {}))
      source = _source_from_meta(meta)
      tags = _tags_from_meta(meta)
      if (
        cutoff
        and dt < cutoff
        and source in ("auto", "forced", "unknown")
        and "keep" not in tags
        and not _is_retention_protected(path)
      ):
        try:
          _remove_with_sidecars(path)
        except Exception as exc:
          report_swallowed_exception(
            exc,
            context="billing_invoices.admin._cleanup_old_backups.remove_backup",
            log_key="billing_invoices.admin._cleanup_old_backups.remove_backup",
            log_window_seconds=300,
          )

  # Count-based cleanup (protect disk usage when many backups are created in a short time).
  if max_count and max_count > 0:
    try:
      eligible = []
      for dt, path, meta in backup_items:
        if not os.path.exists(path):
          continue
        source = _source_from_meta(meta)
        tags = _tags_from_meta(meta)
        if "keep" in tags:
          continue
        if _is_retention_protected(path):
          continue
        if source == "manual":
          continue
        eligible.append((dt, path))
      eligible.sort(key=lambda x: x[0])
      while len(eligible) > max_count:
        _dt, p = eligible.pop(0)
        try:
          _remove_with_sidecars(p)
        except Exception as exc:
          report_swallowed_exception(
            exc,
            context="billing_invoices.admin._cleanup_old_backups.max_count.remove_backup",
            log_key="billing_invoices.admin._cleanup_old_backups.max_count.remove_backup",
            log_window_seconds=300,
          )
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.admin._cleanup_old_backups.max_count",
        log_key="billing_invoices.admin._cleanup_old_backups.max_count",
        log_window_seconds=300,
      )

  if attachments_max_count and attachments_max_count > 0:
    try:
      eligible = []
      for dt, path, meta in attachment_items:
        if not os.path.exists(path):
          continue
        tags = _tags_from_meta(meta)
        if "keep" in tags:
          continue
        if _is_retention_protected(path):
          continue
        eligible.append((dt, path))
      eligible.sort(key=lambda x: x[0])
      while len(eligible) > attachments_max_count:
        _dt, p = eligible.pop(0)
        try:
          _remove_with_sidecars(p)
        except Exception as exc:
          report_swallowed_exception(
            exc,
            context="billing_invoices.admin._cleanup_old_backups.max_count.remove_attachments_zip",
            log_key="billing_invoices.admin._cleanup_old_backups.max_count.remove_attachments_zip",
            log_window_seconds=300,
          )
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.admin._cleanup_old_backups.max_count.attachments",
        log_key="billing_invoices.admin._cleanup_old_backups.max_count.attachments",
        log_window_seconds=300,
      )

  # Byte-based cleanup (hard cap). This is a safety valve to prevent runaway disk usage.
  # Protects any item tagged with "keep". If everything is "keep", we may remain over cap.
  if max_bytes and max_bytes > 0:
    try:
      total = 0
      eligible = []
      for dt, path, meta in backup_items:
        if not os.path.exists(path):
          continue
        try:
          size = int(os.path.getsize(path) or 0)
        except Exception:
          size = 0
        total += size
        meta_dict = meta if isinstance(meta, dict) else {}
        tags = _tags_from_meta(meta_dict)
        if "keep" in tags:
          continue
        if _is_retention_protected(path):
          continue
        source = _source_from_meta(meta_dict)
        is_manual = source == "manual"
        # Prefer deleting non-manual backups first; manual backups are often more intentional.
        eligible.append((is_manual, dt, path, size))
      eligible.sort(key=lambda x: (x[0], x[1]))
      while total > max_bytes and eligible:
        _is_manual, _dt, p, size = eligible.pop(0)
        try:
          _remove_with_sidecars(p)
          total -= int(size or 0)
        except Exception as exc:
          report_swallowed_exception(
            exc,
            context="billing_invoices.admin._cleanup_old_backups.max_bytes.remove_backup",
            log_key="billing_invoices.admin._cleanup_old_backups.max_bytes.remove_backup",
            log_window_seconds=300,
          )
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.admin._cleanup_old_backups.max_bytes",
        log_key="billing_invoices.admin._cleanup_old_backups.max_bytes",
        log_window_seconds=300,
      )

  if attachments_max_bytes and attachments_max_bytes > 0:
    try:
      total = 0
      eligible = []
      for dt, path, meta in attachment_items:
        if not os.path.exists(path):
          continue
        try:
          size = int(os.path.getsize(path) or 0)
        except Exception:
          size = 0
        total += size
        meta_dict = meta if isinstance(meta, dict) else {}
        tags = _tags_from_meta(meta_dict)
        if "keep" in tags:
          continue
        if _is_retention_protected(path):
          continue
        source = _source_from_meta(meta_dict)
        is_manual = source == "manual"
        eligible.append((is_manual, dt, path, size))
      eligible.sort(key=lambda x: (x[0], x[1]))
      while total > attachments_max_bytes and eligible:
        _is_manual, _dt, p, size = eligible.pop(0)
        try:
          _remove_with_sidecars(p)
          total -= int(size or 0)
        except Exception as exc:
          report_swallowed_exception(
            exc,
            context="billing_invoices.admin._cleanup_old_backups.max_bytes.remove_attachment",
            log_key="billing_invoices.admin._cleanup_old_backups.max_bytes.remove_attachment",
            log_window_seconds=300,
          )
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.admin._cleanup_old_backups.max_bytes.attachments",
        log_key="billing_invoices.admin._cleanup_old_backups.max_bytes.attachments",
        log_window_seconds=300,
      )

  if transfer_max_count and transfer_max_count > 0:
    try:
      eligible = []
      for dt, path, meta in transfer_items:
        if not os.path.exists(path):
          continue
        tags = _tags_from_meta(meta)
        if "keep" in tags:
          continue
        if _is_retention_protected(path):
          continue
        eligible.append((dt, path))
      eligible.sort(key=lambda x: x[0])
      while len(eligible) > transfer_max_count:
        _dt, p = eligible.pop(0)
        try:
          _remove_transfer_with_sidecars(p)
        except Exception as exc:
          report_swallowed_exception(
            exc,
            context="billing_invoices.admin._cleanup_old_backups.max_count.remove_transfer_bundle",
            log_key="billing_invoices.admin._cleanup_old_backups.max_count.remove_transfer_bundle",
            log_window_seconds=300,
          )
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.admin._cleanup_old_backups.max_count.transfer",
        log_key="billing_invoices.admin._cleanup_old_backups.max_count.transfer",
        log_window_seconds=300,
      )

  if transfer_max_bytes and transfer_max_bytes > 0:
    try:
      total = 0
      eligible = []
      for dt, path, meta in transfer_items:
        if not os.path.exists(path):
          continue
        try:
          size = int(os.path.getsize(path) or 0)
        except Exception:
          size = 0
        total += size
        tags = _tags_from_meta(meta if isinstance(meta, dict) else {})
        if "keep" in tags:
          continue
        if _is_retention_protected(path):
          continue
        eligible.append((dt, path, size))
      eligible.sort(key=lambda x: x[0])
      while total > transfer_max_bytes and eligible:
        _dt, p, size = eligible.pop(0)
        try:
          _remove_transfer_with_sidecars(p)
          total -= int(size or 0)
        except Exception as exc:
          report_swallowed_exception(
            exc,
            context="billing_invoices.admin._cleanup_old_backups.max_bytes.remove_transfer_bundle",
            log_key="billing_invoices.admin._cleanup_old_backups.max_bytes.remove_transfer_bundle",
            log_window_seconds=300,
          )
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.admin._cleanup_old_backups.max_bytes.transfer",
        log_key="billing_invoices.admin._cleanup_old_backups.max_bytes.transfer",
        log_window_seconds=300,
      )


@bp.route("/backup", methods=["POST"])
@role_required("admin")
def create_backup():
  """Create a database backup using pg_dump for PostgreSQL."""
  if not _get_pg_connection_info():
    flash("PostgreSQL Backup .", "warning")
    return redirect(url_for("admin.deletions_page", tab="audit"))

  payload = _admin_job_payload(get_current_user())
  try:
    run_id = _enqueue_admin_job("backup.create", payload)
  except Exception as e:
    current_app.logger.exception("Failed to enqueue admin backup job.")
    flash(f"Backup Actions Registration : {e}", "error")
    return redirect(url_for("admin.deletions_page", tab="audit"))

  _run_admin_job_background(run_id, "backup.create", payload)
  flash(f"Backup Actions . Actions ID: {run_id}", "info")
  return redirect(url_for("admin.deletions_page", tab="audit"))


def _zip_attachments(ts: str) -> str:
  """Create a zip archive of file storage roots and return its path.
  The archive name is attachments-<ts>.zip in BACKUP_DIR.
  Includes:
   - ATTACHMENTS_DIR under "attachments/"
   - UPLOAD_FOLDER under "uploads/"
  """
  backup_dir = _backup_dir()
  roots = [
    ("attachments", current_app.config.get("ATTACHMENTS_DIR")),
    ("uploads", current_app.config.get("UPLOAD_FOLDER")),
  ]
  os.makedirs(backup_dir, exist_ok=True)
  zip_path = os.path.join(backup_dir, f"attachments-{ts}.zip")
  seen_roots: set[str] = set()
  seen_files: set[str] = set()
  try:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
      for prefix, root_path in roots:
        if not root_path:
          continue
        root_real = os.path.realpath(root_path)
        if root_real in seen_roots or not os.path.isdir(root_real):
          continue
        seen_roots.add(root_real)

        for walk_root, _dirs, files in os.walk(root_real):
          for name in files:
            full = os.path.join(walk_root, name)
            full_real = os.path.realpath(full)
            if full_real in seen_files:
              continue
            seen_files.add(full_real)

            rel = os.path.relpath(full, root_real).replace("\\", "/")
            arcname = f"{prefix}/{rel}".replace("//", "/")
            try:
              zf.write(full, arcname)
            except OSError:
              # Skip unreadable files
              continue
  except Exception:
    # If zip failed, try to remove the half-created file
    try:
      if os.path.exists(zip_path):
        os.remove(zip_path)
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.admin._zip_attachments.cleanup_remove",
        log_key="billing_invoices.admin._zip_attachments.cleanup_remove",
        log_window_seconds=300,
      )
    raise
  return zip_path


@bp.route("/backup_full", methods=["POST"])
@role_required("admin")
def create_backup_with_attachments():
  """Create DB backup and a ZIP archive including attachments + uploads."""
  if not _get_pg_connection_info():
    flash("PostgreSQL Backup .", "warning")
    return redirect(url_for("admin.deletions_page", tab="audit"))

  payload = _admin_job_payload(get_current_user())
  try:
    run_id = _enqueue_admin_job("backup.create_full", payload)
  except Exception as e:
    current_app.logger.exception("Failed to enqueue admin backup job.")
    flash(f"Backup Actions Registration : {e}", "error")
    return redirect(url_for("admin.deletions_page", tab="audit"))

  _run_admin_job_background(run_id, "backup.create_full", payload)
  flash(f"/Upload Backup Actions . Actions ID: {run_id}", "info")
  return redirect(url_for("admin.deletions_page", tab="audit"))


@bp.route("/attachments/cleanup", methods=["POST"])
@role_required("admin")
def cleanup_orphan_attachments():
  """Remove files on disk that don't have a corresponding DB record."""
  from app.services.storage.file_asset_service import get_file_asset_service

  # 1. Clean up legacy invoices (attachments dir)
  root = current_app.config.get("ATTACHMENTS_DIR")
  removed = []
  removed_dirs = 0

  if root and os.path.exists(root) and os.path.isdir(root):
    conn = get_db()
    try:
      for entry in os.listdir(root):
        dpath = os.path.join(root, entry)
        if not os.path.isdir(dpath) or not entry.startswith("invoice_"):
          continue
        try:
          inv_id = int(entry.split("_", 1)[1])
        except Exception:
          inv_id = None
        for f in os.listdir(dpath):
          fpath = os.path.join(dpath, f)
          if not os.path.isfile(fpath):
            continue
          ok = False
          try:
            row = conn.execute(
              "SELECT 1 FROM invoice_attachments WHERE invoice_id=? AND stored_name=?",
              (inv_id, f),
            ).fetchone()
            ok = bool(row)
          except Exception:
            ok = True # if DB error, do not delete
          if not ok:
            try:
              os.remove(fpath)
              removed.append(fpath)
            except Exception as exc:
              report_swallowed_exception(
                exc,
                context="billing_invoices.admin.cleanup_orphan_attachments.remove_file",
                log_key="billing_invoices.admin.cleanup_orphan_attachments.remove_file",
                log_window_seconds=300,
              )
        # After file removals, remove empty invoice directory
        try:
          if not os.listdir(dpath):
            os.rmdir(dpath)
            removed_dirs += 1
        except Exception as exc:
          report_swallowed_exception(
            exc,
            context="billing_invoices.admin.cleanup_orphan_attachments.remove_dir",
            log_key="billing_invoices.admin.cleanup_orphan_attachments.remove_dir",
            log_window_seconds=300,
          )
    finally:
      conn.close()

  # 2. Clean up FileAssets (UPLOAD_FOLDER)
  file_service = get_file_asset_service()
  fa_stats = file_service.purge_orphaned_assets(min_age_days=0, limit=2000, dry_run=False)

  # Log audit
  audit_data = {
    "legacy_removed_files": len(removed),
    "legacy_removed_dirs": removed_dirs,
    "file_asset_stats": fa_stats,
  }

  try:
    log_audit(
      "attachments.cleanup",
      "system",
      None,
      json.dumps(audit_data),
    )
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.admin.cleanup_orphan_attachments.log_audit",
      log_key="billing_invoices.admin.cleanup_orphan_attachments.log_audit",
      log_window_seconds=300,
    )

  flash(
    f" File Done: {len(removed)}items, daysFile {fa_stats.get('deleted', 0)}items Delete ( {fa_stats.get('scanned', 0)}items)",
    "success",
  )
  return redirect(url_for("admin.deletions_page", tab="audit"))


@bp.route("/restore", methods=["POST"])
@role_required("admin")
def restore_to_time():
  """Restore DB to the latest backup at or before the given timestamp."""
  at_str = request.form.get("restore_at", "").strip()
  if not at_str:
    flash("Restore  not available.", "error")
    return redirect(url_for("admin.deletions_page", tab="audit"))
  # Parse 'YYYY-MM-DD HH:MM:SS'
  try:
    at = datetime.fromisoformat(at_str.replace("T", " ").split(".")[0])
  except Exception:
    flash(" ", "error")
    return redirect(url_for("admin.deletions_page", tab="audit"))

  backup_dir = _backup_dir()
  # Pick latest backup <= at (support .db/.sql/.dump)
  candidates = []
  for name in os.listdir(backup_dir):
    if not name.startswith("backup-"):
      continue
    if name.endswith(".db"):
      ts = name[len("backup-") : -len(".db")]
      ext = ".db"
    elif name.endswith(".sql"):
      ts = name[len("backup-") : -len(".sql")]
      ext = ".sql"
    elif name.endswith(".dump"):
      ts = name[len("backup-") : -len(".dump")]
      ext = ".dump"
    else:
      continue
    try:
      dt = datetime.strptime(ts, "%Y%m%d%H%M%S")
    except Exception:
      continue
    if dt <= at:
      candidates.append((dt, os.path.join(backup_dir, name), ext))
  if not candidates:
    flash(" Previous Restore not available.", "error")
    return redirect(url_for("admin.deletions_page", tab="audit"))
  candidates.sort()
  chosen_dt, chosen_path, chosen_ext = candidates[-1]

  if not _get_pg_connection_info() or chosen_ext not in (".sql", ".dump"):
    flash("PostgreSQL .sql .dump Backup Restore .", "warning")
    return redirect(url_for("admin.deletions_page", tab="audit"))

  payload = _admin_job_payload(get_current_user())
  payload.update(
    {
      "backup_path": chosen_path,
      "restore_at": at_str,
    }
  )
  try:
    run_id = _enqueue_admin_job("backup.restore", payload)
  except Exception as e:
    current_app.logger.exception("Failed to enqueue admin restore job.")
    flash(f"Restore Actions Registration : {e}", "error")
    return redirect(url_for("admin.deletions_page", tab="audit"))

  _run_admin_job_background(run_id, "backup.restore", payload)
  flash(f"Restore Actions . Actions ID: {run_id}", "info")
  return redirect(url_for("admin.deletions_page", tab="audit"))


# ====================== Programmatic backup helpers for scheduler ======================
def _create_backup_file() -> str:
  """Create a PostgreSQL backup using pg_dump and return the backup file path.
  Raises on failure. Caller is responsible for cleanup/logging.
  """
  backup_dir = _backup_dir()
  ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
  raw_fmt = str(current_app.config.get("BACKUP_PG_FORMAT", "sql") or "sql").strip().lower()
  if raw_fmt in {"dump", "custom", "c"}:
    ext = ".dump"
  else:
    ext = ".sql"
  backup_path = os.path.join(backup_dir, f"backup-{ts}{ext}")
  log_path = _backup_log_path(backup_path)

  pg_info = _get_pg_connection_info()
  if not pg_info:
    raise Exception("PostgreSQL Link not found.")

  env = os.environ.copy()
  env["PGPASSWORD"] = pg_info["password"]

  try:
    no_owner = bool(current_app.config.get("BACKUP_PG_NO_OWNER", True))
  except Exception:
    no_owner = True
  try:
    no_privs = bool(current_app.config.get("BACKUP_PG_NO_PRIVILEGES", True))
  except Exception:
    no_privs = True
  pg_dump_flags = ["--clean", "--if-exists"]
  if no_owner:
    pg_dump_flags.append("--no-owner")
  if no_privs:
    pg_dump_flags.append("--no-privileges")
  if ext == ".dump":
    pg_dump_flags.extend(["-F", "c"])
    compress_spec = str(current_app.config.get("BACKUP_PG_COMPRESS", "") or "").strip()
    if compress_spec:
      # PostgreSQL 18+: -Z accepts METHOD[:DETAIL], e.g. "zstd:level=3".
      # Numeric values are still accepted for backward compat (gzip level).
      pg_dump_flags.extend(["-Z", compress_spec])
    else:
      try:
        level = int(current_app.config.get("BACKUP_PG_COMPRESSION", 6) or 0)
      except Exception:
        level = 6
      level = max(0, min(9, level))
      if level > 0:
        pg_dump_flags.extend(["-Z", str(level)])

  local_cmd = None
  if shutil.which("pg_dump"):
    local_cmd = [
      "pg_dump",
      "-h",
      pg_info["host"],
      "-p",
      pg_info["port"],
      "-U",
      pg_info["username"],
      "-d",
      pg_info["database"],
      *pg_dump_flags,
    ]

  docker_cmd = None
  if shutil.which("docker"):
    container_name = _safe_container_name(os.environ.get("DB_CONTAINER_NAME", "new_IPM-db"))
    docker_cmd = [
      "docker",
      "exec",
      "-i",
      "-e",
      f"PGPASSWORD={pg_info['password']}",
      container_name,
      "pg_dump",
      "-U",
      pg_info["username"],
      "-d",
      pg_info["database"],
      *pg_dump_flags,
    ]

  if not local_cmd and not docker_cmd:
    raise Exception("pg_dump docker not found.")

  def _run_pg_dump(run_cmd: list[str]) -> tuple[subprocess.CompletedProcess, str]:
    if ext == ".dump":
      with open(backup_path, "wb") as f:
        result = subprocess.run(
          run_cmd, env=env, stdout=f, stderr=subprocess.PIPE, text=False, timeout=300
        )
      try:
        stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
      except Exception:
        stderr = str(result.stderr)
      return result, stderr
    with open(backup_path, "w", encoding="utf-8") as f:
      result = subprocess.run(
        run_cmd, env=env, stdout=f, stderr=subprocess.PIPE, text=True, timeout=300
      )
    return result, (result.stderr or "")

  stderr_text = ""
  use_docker = False
  result = None
  try:
    if local_cmd:
      result, stderr_text = _run_pg_dump(local_cmd)
      if (
        result.returncode != 0
        and "server version mismatch" in stderr_text.lower()
        and docker_cmd
      ):
        if os.path.exists(backup_path):
          os.remove(backup_path)
        use_docker = True
        result, stderr_text = _run_pg_dump(docker_cmd)
    else:
      use_docker = True
      result, stderr_text = _run_pg_dump(docker_cmd)
  except Exception:
    if os.path.exists(backup_path):
      try:
        os.remove(backup_path)
      except Exception as cleanup_exc:
        report_swallowed_exception(
          cleanup_exc,
          context="billing_invoices.admin.pg_dump.cleanup_backup",
          log_key="billing_invoices.admin.pg_dump.cleanup_backup",
          log_window_seconds=300,
        )
    raise

  _write_job_log(
    log_path,
    stdout=None,
    stderr=stderr_text,
    header={
      "command": "pg_dump",
      "mode": "docker" if use_docker else "local",
      "backup": os.path.basename(backup_path),
      "format": ext.lstrip("."),
    },
  )

  if result is None or result.returncode != 0:
    if os.path.exists(backup_path):
      os.remove(backup_path)
    raise Exception(f"Backup failed: {stderr_text}")

  try:
    _write_backup_meta(backup_path, source="unknown", trigger="system", log_path=log_path)
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.admin._create_backup_file.write_backup_meta",
      log_key="billing_invoices.admin._create_backup_file.write_backup_meta",
      log_window_seconds=300,
    )

  return backup_path


def _latest_backup_time():
  """Return latest backup timestamp (UTC) from files in BACKUP_DIR, or None."""
  backup_dir = _backup_dir()
  latest = None
  for name in os.listdir(backup_dir):
    # Support both .db (legacy) and .sql (new) formats
    if not name.startswith("backup-"):
      continue
    if name.endswith(".db"):
      ts_str = name[len("backup-") : -len(".db")]
    elif name.endswith(".sql"):
      ts_str = name[len("backup-") : -len(".sql")]
    elif name.endswith(".dump"):
      ts_str = name[len("backup-") : -len(".dump")]
    else:
      continue
    try:
      dt = datetime.strptime(ts_str, "%Y%m%d%H%M%S")
      if not latest or dt > latest:
        latest = dt
    except Exception:
      continue
  return latest


def _db_last_change_time():
  """Return an estimated last change time for PostgreSQL, or None if unknown."""
  return None


def auto_backup_if_changed() -> bool:
  """Create a backup only if DB changed since the latest backup.
  Logs an audit record when a backup is created. Cleans up old backups.
  Returns True if a backup was created, else False.
  """
  try:
    last_backup = _latest_backup_time()
    last_change = _db_last_change_time()
    if last_change is None:
      min_hours = int(current_app.config.get("BACKUP_AUTO_INTERVAL_HOURS", 24) or 24)
      if last_backup and (datetime.utcnow() - last_backup) < timedelta(hours=min_hours):
        return False
      should_backup = True
    else:
      should_backup = (last_backup is None) or (last_change > last_backup)
    if not should_backup:
      return False
    path = _create_backup_file()
    log_path = _backup_log_path(path)
    try:
      log_audit("backup.auto_create", "backup", None, f'{{"path": "{path}"}}')
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.admin.auto_backup_if_changed.log_audit",
        log_key="billing_invoices.admin.auto_backup_if_changed.log_audit",
        log_window_seconds=300,
      )
    try:
      _write_backup_meta(
        path,
        source="auto",
        trigger="scheduler",
        log_path=log_path,
      )
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.admin.auto_backup_if_changed.write_backup_meta",
        log_key="billing_invoices.admin.auto_backup_if_changed.write_backup_meta",
        log_window_seconds=300,
      )
    try:
      _cleanup_old_backups()
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.admin.auto_backup_if_changed.cleanup_old_backups",
        log_key="billing_invoices.admin.auto_backup_if_changed.cleanup_old_backups",
        log_window_seconds=300,
      )
    return True
  except Exception:
    try:
      current_app.logger.exception("Auto backup failed")
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.admin.auto_backup_if_changed.logger_exception",
        log_key="billing_invoices.admin.auto_backup_if_changed.logger_exception",
        log_window_seconds=300,
      )
    return False


@bp.route("/audit_log")
@role_required("admin")
def audit_log():
  params = dict(request.args)
  params["tab"] = "audit"
  return redirect(url_for("admin.deletions_page", **params))


# ====================== Backups timeline & utilities ======================
def _write_backup_meta(
  backup_path,
  source="manual",
  note=None,
  tags=None,
  created_by=None,
  *,
  trigger=None,
  request_id=None,
  log_path=None,
):
  base, _ext = os.path.splitext(backup_path)
  mp = base + ".json"
  meta = {
    "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    "source": source,
    "trigger": trigger,
    "request_id": request_id,
    "note": note,
    "tags": tags or [],
    "created_by": created_by,
    "size_bytes": (os.path.getsize(backup_path) if os.path.exists(backup_path) else None),
    "log_path": os.path.basename(log_path) if log_path else None,
  }
  try:
    with open(mp, "w", encoding="utf-8") as f:
      json.dump(meta, f, ensure_ascii=False, indent=2)
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.admin._write_backup_meta",
      log_key="billing_invoices.admin._write_backup_meta",
      log_window_seconds=300,
    )


def _read_backup_meta(backup_path):
  base, _ext = os.path.splitext(backup_path)
  mp = base + ".json"
  try:
    if os.path.exists(mp):
      with open(mp, "r", encoding="utf-8") as f:
        return json.load(f)
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.admin._read_backup_meta",
      log_key="billing_invoices.admin._read_backup_meta",
      log_window_seconds=300,
    )
  return {}


def _enumerate_backups():
  d = _backup_dir()
  items = []
  for name in sorted([n for n in os.listdir(d) if n.startswith("backup-")], reverse=True):
    path = os.path.join(d, name)
    # Extract UTC timestamp from filename (use stem without extension)
    stem, _ext = os.path.splitext(name)
    ts_str = None
    if stem.startswith("backup-"):
      ts_str = stem[len("backup-") :]
    elif stem.startswith("pre-restore-"):
      ts_str = stem[len("pre-restore-") :]
    dt_utc = None
    dt_kst_str = None
    try:
      if ts_str:
        dt_utc = datetime.strptime(ts_str, "%Y%m%d%H%M%S")
        dt_kst_str = _display_datetime(dt_utc)
    except Exception:
      dt_utc = None
      dt_kst_str = None
    meta = _read_backup_meta(path)
    # Meta created_at (UTC ISO with 'Z' from sidecar) -> ET string
    meta_created_at = None
    meta_created_at_kst_str = None
    try:
      mc = meta.get("created_at")
      if mc:
        # Accept '...Z' or offset-aware
        if mc.endswith("Z"):
          mc = mc[:-1] + "+00:00"
        mdt = datetime.fromisoformat(mc)
        meta_created_at = mdt
        meta_created_at_kst_str = _display_datetime(mdt)
    except Exception:
      meta_created_at = None
      meta_created_at_kst_str = None
    items.append(
      {
        "name": name,
        "path": path,
        "dt": dt_utc,
        "dt_kst_str": dt_kst_str,
        "meta_created_at": meta_created_at,
        "meta_created_at_kst_str": meta_created_at_kst_str,
        "size": os.path.getsize(path) if os.path.exists(path) else None,
        "source": meta.get("source"),
        "note": meta.get("note"),
        "tags": meta.get("tags") or [],
        "is_current": False, # Not supported for PostgreSQL dumps
      }
    )
  return items


@bp.route("/backups")
@role_required("admin")
def list_backups():
  items = _enumerate_backups()
  return render_template("admin/backups.html", backups=items)


@bp.route("/backups/diff")
@role_required("admin")
def backups_diff():
  # Diff not supported for PostgreSQL dumps yet
  return {"tables": {}}


@bp.route("/backups/restore_file", methods=["POST"])
@role_required("admin")
def restore_backup_file():
  name = (request.form.get("name") or "").strip()
  if not name:
    flash("Backup File name required.", "error")
    return redirect(url_for("billing_invoices.admin.list_backups"))
  path = _resolve_backup_path(name)
  if not path or not os.path.exists(path):
    flash("Backup File not found.", "error")
    return redirect(url_for("billing_invoices.admin.list_backups"))

  if path.endswith(".db"):
    flash("SQLite(.db) Backup PostgreSQL from Restore not available.", "error")
    return redirect(url_for("billing_invoices.admin.list_backups"))

  if not _get_pg_connection_info():
    flash("PostgreSQL Backup Restore .", "warning")
    return redirect(url_for("billing_invoices.admin.list_backups"))

  if not (path.endswith(".sql") or path.endswith(".dump")):
    flash("PostgreSQL .sql .dump Backup Restore .", "warning")
    return redirect(url_for("billing_invoices.admin.list_backups"))

  payload = _admin_job_payload(get_current_user())
  payload.update({"backup_path": path, "backup_name": name})
  try:
    run_id = _enqueue_admin_job("backup.restore_file", payload)
  except Exception as e:
    current_app.logger.exception("Failed to enqueue admin restore job.")
    flash(f"Restore Actions Registration : {e}", "error")
    return redirect(url_for("billing_invoices.admin.list_backups"))

  _run_admin_job_background(run_id, "backup.restore_file", payload)
  flash(f"Restore Actions . Actions ID: {run_id}", "info")
  return redirect(url_for("billing_invoices.admin.list_backups"))


@bp.route("/backups/label", methods=["POST"])
@role_required("admin")
def backups_label():
  name = (request.form.get("name") or "").strip()
  note = (request.form.get("note") or "").strip() or None
  tags = (request.form.get("tags") or "").strip()
  tags_list = [t.strip() for t in tags.split(",")] if tags else []
  if not name:
    flash("Backup File name required.", "error")
    return redirect(url_for("billing_invoices.admin.list_backups"))
  path = _resolve_backup_path(name)
  if not path or not os.path.exists(path):
    flash("Backup File not found.", "error")
    return redirect(url_for("billing_invoices.admin.list_backups"))
  meta = _read_backup_meta(path)
  meta["note"] = note
  meta["tags"] = tags_list
  meta["source"] = meta.get("source") or "manual"
  try:
    meta_path = os.path.splitext(path)[0] + ".json"
    with open(meta_path, "w", encoding="utf-8") as f:
      json.dump(meta, f, ensure_ascii=False, indent=2)
    flash("/ .", "success")
  except Exception as e:
    flash(f" : {e}", "error")
  return redirect(url_for("billing_invoices.admin.list_backups"))


@bp.route("/backups/download/<name>")
@role_required("admin")
def backups_download(name):
  path = _resolve_backup_path(name)
  if not path or not os.path.exists(path):
    flash("Backup File not found.", "error")
    return redirect(url_for("billing_invoices.admin.list_backups"))
  return send_file(path, as_attachment=True, download_name=name)


@bp.route("/maintenance/disable", methods=["POST"])
@role_required("admin")
def disable_maintenance():
  """  """
  from ..maintenance import disable_maintenance_mode, force_disable_maintenance_mode

  try:
    if disable_maintenance_mode():
      flash(" .", "success")
    else:
      #   
      if force_disable_maintenance_mode():
        flash("  .", "warning")
      else:
        flash("  ", "error")
  except Exception as e:
    flash(f"  : {e}", "error")

  return redirect(url_for("admin.deletions_page", tab="audit"))
