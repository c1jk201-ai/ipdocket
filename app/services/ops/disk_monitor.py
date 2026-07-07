from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import datetime
from typing import Any

from flask import current_app, has_app_context

from app.extensions import db
from app.ops.models import DiskSample
from app.services.core.config_service import ConfigService
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text

logger = logging.getLogger(__name__)


def _disk_usage(path: str) -> dict:
    du = shutil.disk_usage(path)
    total = int(du.total) if du.total else 0
    free = int(du.free)
    free_pct = round((free / total) * 100.0, 2) if total else None
    return {"path": path, "total_bytes": total, "free_bytes": free, "free_percent": free_pct}


def _is_low_space(info: dict, *, min_free_bytes: int, min_free_percent: float) -> bool:
    free = int(info.get("free_bytes") or 0)
    pct = info.get("free_percent")
    return (min_free_bytes and free < min_free_bytes) or (
        pct is not None and min_free_percent and pct < min_free_percent
    )


def _bytes_to_gb(n: int) -> float:
    try:
        return float(n) / (1024.0**3)
    except Exception:
        return 0.0


def _collect_targets(*, path: str | None = None) -> list[tuple[str, str]]:
    monitor_path = (path or current_app.config.get("DISK_MONITOR_PATH") or "").strip()
    upload_dir = (current_app.config.get("UPLOAD_FOLDER") or "").strip()
    backup_dir = (
        current_app.config.get("BACKUP_DIR") or current_app.config.get("BACKUP_STORAGE_ROOT") or ""
    ).strip()
    client_dir = (current_app.config.get("CLIENT_ATTACHMENTS_DIR") or "").strip()

    raw_targets: list[tuple[str, str]] = []
    if monitor_path:
        raw_targets.append(("monitor_path", monitor_path))
    if upload_dir:
        raw_targets.append(("uploads", upload_dir))
    if backup_dir:
        raw_targets.append(("backups", backup_dir))
    if client_dir:
        raw_targets.append(("client_attachments", client_dir))
    if not raw_targets:
        raw_targets.append(("cwd", os.getcwd()))

    # De-dupe by path while keeping the first label.
    targets: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    for label, candidate_path in raw_targets:
        if not candidate_path or candidate_path in seen_paths:
            continue
        seen_paths.add(candidate_path)
        targets.append((label, candidate_path))
    return targets


def _persist_disk_samples(results: list[dict[str, Any]]) -> int:
    rows: list[DiskSample] = []
    sampled_at = datetime.utcnow()
    for item in results:
        label = str(item.get("label") or "").strip()
        path = str(item.get("path") or "").strip()
        if not label or not path:
            continue

        try:
            total = int(item.get("total_bytes") or 0)
            free = int(item.get("free_bytes") or 0)
        except Exception:
            continue

        if total <= 0 or free < 0:
            continue

        used = max(total - free, 0)
        used_pct = round((used / total) * 100.0, 2) if total else 0.0
        rows.append(
            DiskSample(
                mount_label=label,
                path=path,
                total_bytes=total,
                used_bytes=used,
                free_bytes=free,
                used_pct=used_pct,
                sampled_at=sampled_at,
            )
        )

    if not rows:
        return 0

    try:
        db.session.add_all(rows)
        db.session.commit()
        return len(rows)
    except Exception as exc:
        db.session.rollback()
        report_swallowed_exception(
            exc,
            context="disk_monitor.persist_samples",
            log_key="disk_monitor.persist_samples",
            log_window_seconds=300,
        )
        return 0



def _persist_system_config_value(key: str, value: str) -> None:
    try:
        engine = db.engine
    except Exception:
        return
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text("UPDATE system_config SET value = :value WHERE key = :key"),
                {"key": key, "value": value},
            )
            if result.rowcount == 0:
                conn.execute(
                    text("INSERT INTO system_config (key, value) VALUES (:key, :value)"),
                    {"key": key, "value": value},
                )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="disk_monitor._persist_system_config_value",
            log_key=f"disk_monitor._persist_system_config_value.{key}",
            log_window_seconds=300,
        )
        try:
            current_app.logger.warning("disk_monitor: failed to persist %s", key)
        except Exception as log_exc:
            report_swallowed_exception(
                log_exc,
                context="disk_monitor._persist_system_config_value.logger_warning",
                log_key="disk_monitor._persist_system_config_value.logger_warning",
                log_window_seconds=300,
            )


def check_disk_and_alert(*, path: str | None = None) -> dict[str, Any]:
    """
    Periodic disk usage monitor + low-disk email alert.
    - throttled via SystemConfig (best-effort commit to persist cooldown across calls)
    """
    if not has_app_context():
        return {"enabled": False, "reason": "no_app_context"}

    enabled = bool(current_app.config.get("DISK_MONITOR_ENABLED", False))
    if not enabled:
        return {"enabled": False}

    try:
        # Backward/forward compatible config:
        # - New keys: DISK_MIN_FREE_* (this module)
        # - Legacy key used by config.py: DISK_ALERT_THRESHOLD_FREE_PERCENT
        min_free_bytes = int(current_app.config.get("DISK_MIN_FREE_BYTES") or 0) or (
            1024 * 1024 * 1024
        )  # default 1GB
        min_free_pct = float(current_app.config.get("DISK_MIN_FREE_PERCENT") or 0) or float(
            current_app.config.get("DISK_ALERT_THRESHOLD_FREE_PERCENT") or 0
        )
        if not min_free_pct:
            min_free_pct = 10.0  # safer default than 3% (avoid "disk full" surprises)

        targets = _collect_targets(path=path)
        results = []
        for label, p in targets:
            try:
                os.makedirs(p, exist_ok=True)
                writable = os.access(p, os.W_OK)
                info = _disk_usage(p)
                info["label"] = label
                info["writable"] = bool(writable)
                info["low_space"] = _is_low_space(
                    info, min_free_bytes=min_free_bytes, min_free_percent=min_free_pct
                )
                if not writable:
                    info["status"] = "not_writable"
                elif info["low_space"]:
                    info["status"] = "low_space"
                else:
                    info["status"] = "ok"
                results.append(info)
            except Exception as e:
                results.append({"label": label, "path": p, "status": f"error:{type(e).__name__}"})

        samples_written = _persist_disk_samples(results)
        email_raw = (current_app.config.get("DISK_ALERT_EMAILS") or "").strip() or (
            current_app.config.get("ERROR_REPORT_ALERT_EMAILS") or ""
        ).strip()
        alerts_configured = bool(email_raw)
        if not alerts_configured:
            logger.warning(
                "Disk monitor: no alert email recipients configured."
            )

        # low_space / not_writable   Notice/Error Process
        if any(r.get("status") in {"low_space", "not_writable"} for r in results):
            logger.error("Disk monitor unhealthy: %s", results)

            # Throttle alerts
            key = "DISK_ALERT_LAST_SENT_AT"
            cooldown_min = int(current_app.config.get("DISK_ALERT_COOLDOWN_MINUTES", 60) or 60)
            now_ts = time.time()
            last_raw = (ConfigService.get_str(key, "", strip=True) or "").strip()
            try:
                last_ts = float(last_raw) if last_raw else 0.0
            except Exception:
                last_ts = 0.0

            if last_ts and (now_ts - last_ts) < float(cooldown_min * 60):
                logger.info("Disk alert suppressed due to cooldown.")
                return {
                    "enabled": True,
                    "ok": False,
                    "suppressed": True,
                    "cooldown_minutes": cooldown_min,
                    "min_free_bytes": min_free_bytes,
                    "min_free_percent": min_free_pct,
                    "samples_written": samples_written,
                    "results": results,
                }

            _persist_system_config_value(key, str(now_ts))

            # Build alert text
            lines = ["🚨 **Disk Monitor Alert**"]
            for r in results:
                if r.get("status") not in {"low_space", "not_writable"}:
                    continue
                lines.append(f"- Path: {r['path']}")
                lines.append(f"  Status: {r['status']}")
                if "free_percent" in r:
                    lines.append(f"  Free: {r['free_percent']}%")
            text = "\n".join(lines)
            # Send alert email
            recipients = [e.strip() for e in email_raw.split(",") if e.strip()]
            email_sent = False
            if recipients:
                try:
                    from app.services.ops.error_report_monitor import _send_error_email

                    _send_error_email(
                        subject="[Disk] Low disk space", body=text, recipients=recipients
                    )
                    email_sent = True
                except Exception as exc:
                    report_swallowed_exception(
                        exc, context="disk_monitor.send_email", log_key="disk_monitor.email"
                    )

            return {
                "enabled": True,
                "ok": False,
                "suppressed": False,
                "cooldown_minutes": cooldown_min,
                "min_free_bytes": min_free_bytes,
                "min_free_percent": min_free_pct,
                "samples_written": samples_written,
                "alert_sent": {"email": email_sent},
                "results": results,
            }

        logger.info("Disk monitor ok: %s", results)
        return {
            "enabled": True,
            "ok": True,
            "min_free_bytes": min_free_bytes,
            "min_free_percent": min_free_pct,
            "samples_written": samples_written,
            "results": results,
        }

    except Exception as exc:
        return {"enabled": True, "ok": False, "error": str(exc)}
