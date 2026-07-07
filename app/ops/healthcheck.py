from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, text


def _database_url() -> str:
    url = (
        os.environ.get("DATABASE_URL") or os.environ.get("SQLALCHEMY_DATABASE_URI") or ""
    ).strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    return url


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key) or default)
    except Exception:
        return default


def _parse_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _age_seconds(dt: datetime) -> float:
    return (datetime.utcnow() - dt).total_seconds()


def _engine():
    url = _database_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return create_engine(url, pool_pre_ping=True)


def _check_scheduler(args) -> int:
    interval = _env_int("SCHEDULER_HEARTBEAT_INTERVAL_SECONDS", 300)
    default_max_age = max(600, int(interval * 2.5))
    max_age = args.max_age_seconds or _env_int(
        "SCHEDULER_HEALTHCHECK_MAX_AGE_SECONDS", default_max_age
    )
    engine = _engine()
    try:
        with engine.connect() as conn:
            last = conn.execute(
                text(
                    """
                    SELECT finished_at
                    FROM job_runs
                    WHERE job_name = 'scheduler_heartbeat'
                      AND status = 'success'
                    ORDER BY finished_at DESC
                    LIMIT 1
                    """
                )
            ).scalar()
    finally:
        engine.dispose()

    last_dt = _parse_datetime(last)
    if last_dt is None:
        print("scheduler heartbeat missing", file=sys.stderr)
        return 1
    age = _age_seconds(last_dt)
    if age > max_age:
        print(f"scheduler heartbeat stale age={age:.0f}s max_age={max_age}s", file=sys.stderr)
        return 1
    print(f"scheduler heartbeat ok age={age:.0f}s")
    return 0


def _check_worker(args) -> int:
    interval = _env_int("WORKER_HEARTBEAT_INTERVAL_SECONDS", 30)
    default_max_age = max(120, interval * 4)
    max_age = args.max_age_seconds or _env_int(
        "WORKER_HEALTHCHECK_MAX_AGE_SECONDS", default_max_age
    )
    hostname = (args.hostname or socket.gethostname()).strip()
    key = f"ops.worker_heartbeat.{hostname}"

    engine = _engine()
    try:
        with engine.connect() as conn:
            raw_value = conn.execute(
                text("SELECT value FROM system_config WHERE key = :key"),
                {"key": key},
            ).scalar()
    finally:
        engine.dispose()

    if not raw_value:
        print(f"worker heartbeat missing key={key}", file=sys.stderr)
        return 1

    try:
        payload = json.loads(raw_value)
    except Exception:
        print(f"worker heartbeat invalid json key={key}", file=sys.stderr)
        return 1

    updated_at = _parse_datetime(payload.get("updated_at"))
    if updated_at is None:
        print(f"worker heartbeat missing updated_at key={key}", file=sys.stderr)
        return 1

    age = _age_seconds(updated_at)
    if age > max_age:
        print(
            f"worker heartbeat stale age={age:.0f}s max_age={max_age}s key={key}", file=sys.stderr
        )
        return 1
    print(f"worker heartbeat ok age={age:.0f}s key={key}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="IP Docket System container healthchecks")
    parser.add_argument("target", choices=("scheduler", "worker"))
    parser.add_argument("--max-age-seconds", type=int, default=0)
    parser.add_argument("--hostname", default="")
    args = parser.parse_args(argv)

    try:
        if args.target == "scheduler":
            return _check_scheduler(args)
        if args.target == "worker":
            return _check_worker(args)
    except Exception as exc:
        print(f"{args.target} healthcheck error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
