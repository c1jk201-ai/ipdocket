from __future__ import annotations

import os
import sys
import time
from urllib.parse import urlparse


def _db_url() -> str:
    return (
        os.environ.get("DATABASE_URL") or os.environ.get("SQLALCHEMY_DATABASE_URI") or ""
    ).strip()


def _is_postgres_url(db_url: str) -> bool:
    if not db_url:
        return False
    try:
        scheme = (urlparse(db_url).scheme or "").strip().lower()
    except Exception:
        scheme = ""
    # Accept postgres://, postgresql://, postgresql+psycopg2:// ...
    return scheme.startswith("postgres")


def _wait_for_db(db_url: str, timeout: int) -> int:
    try:
        import psycopg2
    except Exception as exc:
        print(f"psycopg2 missing: {exc}", file=sys.stderr)
        return 1

    deadline = time.time() + max(1, int(timeout))
    last_err = None
    while time.time() < deadline:
        try:
            # Avoid long hangs on DNS/TCP issues: force a small connect timeout.
            conn = psycopg2.connect(db_url, connect_timeout=5)
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("SELECT 1;")
            cur.close()
            conn.close()
            return 0
        except Exception as exc:
            last_err = exc
            time.sleep(2)

    print(f"DB not reachable within timeout ({timeout}s): {last_err}", file=sys.stderr)
    return 1


def main() -> int:
    db_url = _db_url()
    if not _is_postgres_url(db_url):
        return 0

    timeout = int(os.environ.get("DB_WAIT_TIMEOUT_SECONDS", "120") or "120")
    return _wait_for_db(db_url, timeout)


if __name__ == "__main__":
    sys.exit(main())
