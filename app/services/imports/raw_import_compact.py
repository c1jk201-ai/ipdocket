from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

from app.extensions import db
from app.utils.policy_sql import policy_text as text


def _ensure_payload_schema() -> None:
    """Validate raw_import payload schema exists (PostgreSQL compatible)."""
    # Check if raw_import_row table exists
    result = db.session.execute(
        text(
            """
        SELECT table_name FROM information_schema.tables
        WHERE table_name = 'raw_import_row' AND table_schema = 'public'
    """
        )
    ).fetchone()

    if not result:
        return

    # Check if payload_hash column exists
    cols_result = db.session.execute(
        text(
            """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'raw_import_row' AND table_schema = 'public'
    """
        )
    ).fetchall()
    cols = [r[0] for r in cols_result]

    if "payload_hash" not in cols:
        raise RuntimeError(
            "raw_import_row.payload_hash column missing; apply migrations before compacting"
        )

    payload_tbl = db.session.execute(
        text(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_name = 'raw_import_payload' AND table_schema = 'public'
            """
        )
    ).fetchone()
    if not payload_tbl:
        raise RuntimeError("raw_import_payload table missing; apply migrations before compacting")


def _iter_chunks(items: list[tuple], size: int) -> Iterable[list[tuple]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def compact_raw_import_rows(
    *,
    db_path: Path = None,  # Ignored, uses SQLAlchemy session
    chunk_size: int = 500,
    prune_row_json: bool = True,
    limit_rows: int | None = None,
    drop_row_json_index: bool = False,
    vacuum: bool = False,
) -> dict[str, int]:
    """
    De-duplicate raw_import_row.row_json into raw_import_payload and optionally
    prune raw_import_row.row_json to empty strings to save space.

    Now uses SQLAlchemy for PostgreSQL compatibility.
    """
    try:
        _ensure_payload_schema()

        # Check if raw_import_row table exists
        result = db.session.execute(
            text(
                """
            SELECT table_name FROM information_schema.tables
            WHERE table_name = 'raw_import_row' AND table_schema = 'public'
        """
            )
        ).fetchone()

        if not result:
            return {"processed_rows": 0, "payload_inserts": 0, "updated_rows": 0}

        # Fetch rows that need processing
        rows = db.session.execute(
            text(
                """
            SELECT raw_id, row_json
            FROM raw_import_row
            WHERE (payload_hash IS NULL OR payload_hash = '')
              AND row_json IS NOT NULL
              AND row_json != ''
        """
            )
        ).fetchall()

        processed = 0
        inserted_payloads = 0
        updated_rows = 0

        buffer_payloads: list[tuple[str, str]] = []
        buffer_updates: list[tuple] = []

        def flush():
            nonlocal inserted_payloads, updated_rows, buffer_payloads, buffer_updates
            if buffer_payloads:
                for payload_hash, payload in buffer_payloads:
                    db.session.execute(
                        text(
                            """
                            INSERT INTO raw_import_payload (payload_hash, payload_json)
                            VALUES (:hash, :json)
                            ON CONFLICT DO NOTHING
                        """
                        ),
                        {"hash": payload_hash, "json": payload},
                    )
                    inserted_payloads += 1
                buffer_payloads = []

            if buffer_updates:
                for update_tuple in buffer_updates:
                    if prune_row_json:
                        payload_hash, _, raw_id = update_tuple
                        db.session.execute(
                            text(
                                """
                                UPDATE raw_import_row
                                SET payload_hash = :hash, row_json = ''
                                WHERE raw_id = :raw_id
                            """
                            ),
                            {"hash": payload_hash, "raw_id": raw_id},
                        )
                    else:
                        payload_hash, raw_id = update_tuple
                        db.session.execute(
                            text(
                                """
                                UPDATE raw_import_row
                                SET payload_hash = :hash
                                WHERE raw_id = :raw_id
                            """
                            ),
                            {"hash": payload_hash, "raw_id": raw_id},
                        )
                updated_rows += len(buffer_updates)
                buffer_updates = []

            db.session.commit()

        for row in rows:
            raw_id, row_json = row[0], row[1]
            processed += 1
            if limit_rows and processed > limit_rows:
                break
            raw_id = (raw_id or "").strip()
            if not raw_id:
                continue
            payload = row_json or ""
            payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            buffer_payloads.append((payload_hash, payload))
            if prune_row_json:
                buffer_updates.append((payload_hash, "", raw_id))
            else:
                buffer_updates.append((payload_hash, raw_id))

            if len(buffer_payloads) >= chunk_size:
                flush()

        flush()

        if drop_row_json_index:
            raise RuntimeError(
                "Dropping runtime indexes is disabled; use migrations or maintenance scripts"
            )

        # PostgreSQL uses VACUUM differently - typically not needed in normal operation
        # as autovacuum handles this

        return {
            "processed_rows": processed,
            "payload_inserts": inserted_payloads,
            "updated_rows": updated_rows,
        }
    except Exception as e:
        db.session.rollback()
        return {"processed_rows": 0, "payload_inserts": 0, "updated_rows": 0, "error": str(e)}
