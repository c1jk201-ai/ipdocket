"""CI smoke test: ensure /health and /ready return 200."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    os.environ["FLASK_ENV"] = "testing"
    os.environ["ENV"] = "testing"
    os.environ["APP_ENV"] = "testing"
    os.environ.setdefault("STARTUP_CHECKS_ENFORCE", "0")
    os.environ.setdefault("RATELIMIT_STORAGE_URI", "memory://")
    os.environ.setdefault("RATELIMIT_REQUIRE_SHARED_STORAGE", "0")
    os.environ.setdefault("INVOICEAPP_INTEGRATED", "0")
    os.environ.setdefault("READY_CHECK_DB_OBJECTS", "0")
    os.environ.setdefault("READY_CHECK_MIGRATIONS", "0")

    db_url = (
        os.environ.get("DATABASE_URL") or os.environ.get("SQLALCHEMY_DATABASE_URI") or ""
    ).strip()
    if not db_url:
        sys.stderr.write("DATABASE_URL is required for smoke test.\n")
        return 2

    from app import create_app

    app = create_app("default")
    client = app.test_client()

    health = client.get("/health")
    if health.status_code != 200:
        sys.stderr.write(f"/health failed: {health.status_code}\n")
        return 1

    ready = client.get("/ready")
    if ready.status_code != 200:
        try:
            payload = ready.get_json()
        except Exception:
            payload = ready.data.decode("utf-8", errors="ignore")
        sys.stderr.write(f"/ready failed: {ready.status_code} {payload}\n")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
