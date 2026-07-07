from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta

from flask import Flask, jsonify

from app.core.setup.db_guards import check_migrations_status, check_required_db_objects_status
from app.core.setup.logging_setup import _log_swallowed
from app.extensions import db, limiter
from app.services.billing.subsystem import billing_subsystem_enabled, billing_subsystem_ready
from app.utils.policy_sql import policy_text as text


def register_health_endpoints(app: Flask) -> None:
    # NOTE:
    # - /health: liveness (should be cheap; does not enforce DB readiness)
    # - /ready : readiness status only (503 if not ready)
    # - /internal/ready : detailed readiness checks for internal operators/probes
    #
    # Also exempt from rate limiting so k8s/ELB health probes don't burn quota.

    @app.get("/health")
    @limiter.exempt
    def health():
        return jsonify({"status": "ok"}), 200

    def _scheduler_should_run_here() -> bool:
        scheduler_enabled = bool(app.config.get("SCHEDULER_ENABLED", True))
        if not scheduler_enabled:
            return False
        # Process-level enable flag. Prefer RUN_SCHEDULER (documented), but keep
        # legacy env fallback for compatibility.
        run_scheduler = bool(app.config.get("RUN_SCHEDULER")) or (
            os.environ.get("SCHEDULER_RUN_ANYWAY") == "1"
        )
        if not run_scheduler:
            return False
        if not app.debug:
            role = (os.environ.get("SCHEDULER_PROCESS_ROLE") or "").strip().lower()
            if role != "worker":
                return False
        return True

    def _collect_ready_checks() -> tuple[bool, dict[str, object]]:
        # In tests, avoid hard dependencies (DB/service containers) in readiness checks.
        if bool(app.config.get("TESTING")) or (os.environ.get("TESTING") == "1"):
            return True, {"testing": True}

        checks: dict[str, object] = {}
        ok = True

        # --- DB connectivity ---
        try:
            db.session.execute(text("SELECT 1")).scalar()
            checks["db"] = "ok"
        except Exception as e:
            ok = False
            checks["db"] = f"error:{type(e).__name__}"
            try:
                db.session.rollback()
            except Exception as exc:
                _log_swallowed("ready.rollback", exc)

        # --- Migration status (disabled in the source-only distribution) ---
        try:
            mig = check_migrations_status(app)
            checks["migrations"] = mig
            if (
                app.config.get("STARTUP_CHECKS_ENFORCE")
                and mig.get("enabled")
                and (mig.get("ok") is False)
            ):
                ok = False
        except Exception as exc:
            ok = False
            checks["migrations"] = f"error:{type(exc).__name__}"
            _log_swallowed("ready.migrations", exc)

        # --- Required DB objects (critical views) ---
        try:
            db_objects = check_required_db_objects_status(app)
            checks["db_objects"] = db_objects
            if (
                app.config.get("STARTUP_CHECKS_ENFORCE")
                and db_objects.get("enabled")
                and (db_objects.get("ok") is False)
            ):
                ok = False
        except Exception as exc:
            ok = False
            checks["db_objects"] = f"error:{type(exc).__name__}"
            _log_swallowed("ready.db_objects", exc)

        # --- Durable queue backlog / stale running jobs ---
        try:
            queue_rows = (
                db.session.execute(
                    text(
                        """
                        SELECT queue,
                               status,
                               COUNT(*) AS count,
                               MIN(run_at) AS oldest_run_at,
                               MIN(created_at) AS oldest_created_at
                          FROM durable_jobs
                         WHERE status IN ('queued', 'running', 'failed')
                         GROUP BY queue, status
                         ORDER BY queue, status
                        """
                    )
                )
                .mappings()
                .all()
            )
            ttl_seconds = int(app.config.get("DURABLE_QUEUE_LOCK_TTL_SECONDS", 600) or 600)
            stale_after_seconds = max(ttl_seconds * 2, ttl_seconds + 300)
            stale_cutoff = datetime.utcnow() - timedelta(seconds=stale_after_seconds)
            stale_running = db.session.execute(
                text(
                    """
                    SELECT COUNT(*)
                      FROM durable_jobs
                     WHERE status = 'running'
                       AND locked_at IS NOT NULL
                       AND locked_at < :cutoff
                    """
                ),
                {"cutoff": stale_cutoff},
            ).scalar()
            checks["durable_queue"] = {
                "status_counts": [
                    {
                        "queue": row["queue"],
                        "status": row["status"],
                        "count": int(row["count"] or 0),
                        "oldest_run_at": (
                            row["oldest_run_at"].isoformat()
                            if row["oldest_run_at"] is not None
                            else None
                        ),
                        "oldest_created_at": (
                            row["oldest_created_at"].isoformat()
                            if row["oldest_created_at"] is not None
                            else None
                        ),
                    }
                    for row in queue_rows
                ],
                "stale_running": int(stale_running or 0),
                "stale_after_seconds": stale_after_seconds,
            }
        except Exception as exc:
            checks["durable_queue"] = f"error:{type(exc).__name__}"
            _log_swallowed("ready.durable_queue", exc)

        # --- Upload directory (files/attachments) ---
        upload_dir = (app.config.get("UPLOAD_FOLDER") or "").strip()
        if not upload_dir:
            ok = False
            checks["upload_dir"] = "missing"
        else:
            try:
                os.makedirs(upload_dir, exist_ok=True)
                if os.access(upload_dir, os.W_OK):
                    checks["upload_dir"] = "ok"
                    # Disk space guard (prevents "disk full" silent corruption)
                    if bool(app.config.get("READY_CHECK_UPLOAD_DISK_SPACE", True)):
                        try:
                            du = shutil.disk_usage(upload_dir)
                            free_bytes = int(du.free)
                            total_bytes = int(du.total) if du.total else 0
                            free_pct = (
                                round((free_bytes / total_bytes) * 100.0, 2)
                                if total_bytes
                                else None
                            )
                            min_free_bytes = int(
                                app.config.get("READY_UPLOAD_MIN_FREE_BYTES") or 0
                            ) or (512 * 1024 * 1024)
                            min_free_pct = (
                                float(app.config.get("READY_UPLOAD_MIN_FREE_PERCENT") or 0) or 2.0
                            )
                            checks["upload_disk"] = {
                                "free_bytes": free_bytes,
                                "total_bytes": total_bytes,
                                "free_percent": free_pct,
                                "min_free_bytes": min_free_bytes,
                                "min_free_percent": min_free_pct,
                            }
                            if (min_free_bytes and free_bytes < min_free_bytes) or (
                                (free_pct is not None)
                                and min_free_pct
                                and (free_pct < min_free_pct)
                            ):
                                ok = False
                                checks["upload_disk"]["status"] = "low_space"
                        except Exception as e:
                            checks["upload_disk"] = f"error:{type(e).__name__}"
                else:
                    ok = False
                    checks["upload_dir"] = "not_writable"
            except Exception as e:
                ok = False
                checks["upload_dir"] = f"error:{type(e).__name__}"

        # --- Invoice integration readiness (if enabled) ---
        inv_integrated = billing_subsystem_enabled(app)
        inv_ok = billing_subsystem_ready(app)
        checks["invoice"] = "ok" if inv_ok else "not_ready"
        if inv_integrated and not inv_ok:
            ok = False

        # --- Scheduler readiness (only if this process is supposed to run it) ---
        sched_should = _scheduler_should_run_here()
        sched_running = app.extensions.get("apscheduler") is not None
        sched_info: dict[str, object] = {
            "should_run": bool(sched_should),
            "running": bool(sched_running),
        }
        if sched_running:
            lock_conn = app.extensions.get("apscheduler_lock_conn")
            lock_file = app.extensions.get("apscheduler_lock_file")
            sched_info["lock"] = {
                "postgres": bool(lock_conn is not None),
                "file": bool(lock_file is not None),
                "backend_pid": app.extensions.get("apscheduler_lock_backend_pid"),
                "key": app.extensions.get("apscheduler_lock_key"),
            }

            # Scheduler heartbeat check.
            try:
                from app.models.job_run import JobRun

                hb_interval = int(
                    app.config.get("SCHEDULER_HEARTBEAT_INTERVAL_SECONDS", 300) or 300
                )
                lookback = datetime.utcnow() - timedelta(seconds=max(600, hb_interval * 2.5))

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

                sched_info["heartbeat"] = {
                    "ok": bool(last_hb),
                    "last_success": last_hb.isoformat() if last_hb else None,
                }

                if sched_should and app.config.get("SCHEDULER_HEARTBEAT_ENFORCE", False):
                    if not last_hb:
                        ok = False
            except Exception as exc:
                sched_info["heartbeat_error"] = str(exc)
                _log_swallowed("ready.scheduler_heartbeat", exc)

        checks["scheduler"] = sched_info
        if sched_should and not sched_running:
            ok = False

        # --- Startup checks (SystemConfig keys, paths, integrations) ---
        try:
            from app.utils.db_startup import collect_startup_checks

            startup_ok, startup_checks = collect_startup_checks(app)
            startup_checks["ok"] = startup_ok
            checks["startup"] = startup_checks
            if app.config.get("STARTUP_CHECKS_ENFORCE") and not startup_ok:
                ok = False
        except Exception as exc:
            ok = False
            checks["startup"] = f"error:{type(exc).__name__}"
            _log_swallowed("ready.startup_checks", exc)

        # --- Operational safety signals ---
        try:
            from app.services.ops.operational_metrics import collect_operational_metrics

            ops_metrics = collect_operational_metrics()
            checks["operational_metrics"] = {
                "db_pool": ops_metrics.get("db_pool"),
                "durable_queue": (ops_metrics.get("durable_queue") or {}).get("totals"),
                "heartbeats": ops_metrics.get("heartbeats"),
                "migration_drift": ops_metrics.get("migration_drift"),
            }

            db_pool = ops_metrics.get("db_pool") or {}
            util = db_pool.get("utilization") if isinstance(db_pool, dict) else None
            try:
                max_util = float(app.config.get("READY_DB_POOL_MAX_UTILIZATION") or 0)
            except Exception:
                max_util = 0.0
            if util is not None and max_util > 0 and float(util) >= max_util:
                ok = False

            durable_totals = (ops_metrics.get("durable_queue") or {}).get("totals") or {}
            try:
                max_lag = int(app.config.get("READY_MAX_DURABLE_QUEUE_LAG_SECONDS") or 0)
            except Exception:
                max_lag = 0
            if max_lag > 0 and int(durable_totals.get("max_queue_lag_seconds") or 0) > max_lag:
                ok = False

            heartbeats = ops_metrics.get("heartbeats") or {}
            try:
                max_worker_age = int(app.config.get("READY_WORKER_HEARTBEAT_MAX_AGE_SECONDS") or 0)
            except Exception:
                max_worker_age = 0
            newest_worker_age = heartbeats.get("newest_worker_age_seconds")
            if (
                max_worker_age > 0
                and newest_worker_age is not None
                and int(newest_worker_age) > max_worker_age
            ):
                ok = False
        except Exception as exc:
            checks["operational_metrics"] = f"error:{type(exc).__name__}"
            _log_swallowed("ready.operational_metrics", exc)

        return ok, checks

    def _ready_response(*, include_checks: bool):
        ok, checks = _collect_ready_checks()
        status_code = 200 if ok else 503
        payload: dict[str, object] = {"status": "ok" if ok else "not_ready"}
        if include_checks:
            payload["checks"] = checks
        return jsonify(payload), status_code

    @app.get("/ready")
    @limiter.exempt
    def ready():
        include_checks = bool(app.config.get("READY_PUBLIC_INCLUDE_CHECKS", False))
        return _ready_response(include_checks=include_checks)

    @app.get("/internal/ready")
    @limiter.exempt
    def internal_ready():
        return _ready_response(include_checks=True)

    @app.get("/internal/metrics")
    @limiter.exempt
    def internal_metrics():
        from app.services.ops.operational_metrics import collect_operational_metrics

        return jsonify(collect_operational_metrics()), 200
