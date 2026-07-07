from __future__ import annotations

import csv
import json
import re
import uuid
from pathlib import Path
from typing import List

from sqlalchemy import MetaData, Table, case, inspect, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.extensions import db
from app.models.raw_import import RawImportField

_SKIP_SOURCE_COLUMN_RE = re.compile(r"\.\d+$")


def _load_raw_json_only_mapping(csv_path: Path) -> dict[str, set[str]]:
    """
    Returns {sheet_name: {source_column,...}} for rows with:
      target_table=raw_import_row, target_column=row_json, mapping_type=raw_json_only

    Heuristics:
    - Skip 'No'
    - Skip columns ending with '.<digit>' (ex: Filing type.1)
    """
    mapping: dict[str, set[str]] = {}
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if (r.get("target_table") or "").strip() != "raw_import_row":
                continue
            if (r.get("target_column") or "").strip() != "row_json":
                continue
            if (r.get("mapping_type") or "").strip() != "raw_json_only":
                continue
            sheet = (r.get("sheet_name") or "").strip()
            col = (r.get("source_column") or "").strip()
            if not sheet or not col:
                continue
            if col == "No":
                continue
            if _SKIP_SOURCE_COLUMN_RE.search(col):
                continue
            mapping.setdefault(sheet, set()).add(col)
    return mapping


def _ensure_table() -> None:
    insp = inspect(db.engine)
    if "raw_import_field" not in set(insp.get_table_names()):
        raise RuntimeError(
            "raw_import_field table missing; apply migrations before extracting fields"
        )


def extract_raw_json_only_fields(
    *,
    db_path: Path,  # Kept for signature compatibility, ignored
    mapping_csv_path: Path,
    chunk_size: int = 500,
    limit_rows: int | None = None,
) -> dict[str, int]:
    """
    Extracts selected raw_json_only fields from raw_import_row.row_json into raw_import_field.
    This is a one-way materialization for DB-first access; raw_import_row remains unchanged.
    """
    mapping = _load_raw_json_only_mapping(mapping_csv_path)
    sheets = sorted(mapping.keys())
    if not sheets:
        return {"processed_rows": 0, "inserted_or_updated": 0, "sheets": 0, "keys_total": 0}

    _ensure_table()

    # Check for payload table/column via SQLAlchemy inspector (cross-dialect).
    insp = inspect(db.engine)
    tables = set(insp.get_table_names())
    has_payload_table = "raw_import_payload" in tables
    try:
        raw_import_row_cols = {c.get("name") for c in (insp.get_columns("raw_import_row") or [])}
    except Exception:
        raw_import_row_cols = set()
    has_payload_column = "payload_hash" in raw_import_row_cols

    meta = MetaData()
    raw_import_row = Table("raw_import_row", meta, autoload_with=db.engine)
    if has_payload_table and has_payload_column:
        raw_import_payload = Table("raw_import_payload", meta, autoload_with=db.engine)
        row_json_expr = case(
            (
                (raw_import_row.c.row_json.is_(None)) | (raw_import_row.c.row_json == ""),
                raw_import_payload.c.payload_json,
            ),
            else_=raw_import_row.c.row_json,
        ).label("row_json")
        stmt = (
            select(raw_import_row.c.raw_id, raw_import_row.c.sheet_name, row_json_expr)
            .select_from(
                raw_import_row.outerjoin(
                    raw_import_payload,
                    raw_import_payload.c.payload_hash == raw_import_row.c.payload_hash,
                )
            )
            .where(raw_import_row.c.sheet_name.in_(sheets))
        )
    else:
        stmt = select(
            raw_import_row.c.raw_id, raw_import_row.c.sheet_name, raw_import_row.c.row_json
        ).where(raw_import_row.c.sheet_name.in_(sheets))

    rows = db.session.execute(stmt).all()

    inserted = 0
    processed_rows = 0
    buffer: List[dict] = []

    def flush():
        nonlocal inserted, buffer
        if not buffer:
            return
        tbl = RawImportField.__table__
        dialect = (getattr(getattr(db.engine, "dialect", None), "name", "") or "").lower()
        if dialect.startswith("postgres"):
            stmt = pg_insert(tbl).values(buffer)
            stmt = stmt.on_conflict_do_update(
                index_elements=[tbl.c.raw_id, tbl.c.source_column],
                set_={
                    "sheet_name": stmt.excluded.sheet_name,
                    "value_text": stmt.excluded.value_text,
                },
            )
        elif dialect.startswith("sqlite"):
            stmt = sqlite_insert(tbl).values(buffer)
            stmt = stmt.on_conflict_do_update(
                index_elements=[tbl.c.raw_id, tbl.c.source_column],
                set_={
                    "sheet_name": stmt.excluded.sheet_name,
                    "value_text": stmt.excluded.value_text,
                },
            )
        else:
            raise RuntimeError(f"Unsupported DB dialect for raw_import_field upsert: {dialect}")
        db.session.execute(stmt)
        inserted += len(buffer)
        buffer = []

    for row in rows:
        # access via integer index or attribute depending on row proxy
        # row is (raw_id, sheet_name, row_json)
        raw_id = row[0]
        sheet_name = row[1]
        row_json_val = row[2]

        processed_rows += 1
        if limit_rows and processed_rows > limit_rows:
            break

        try:
            obj = json.loads(row_json_val or "{}")
        except Exception:
            obj = {}
        if not isinstance(obj, dict):
            obj = {}

        wanted = mapping.get(sheet_name) or set()
        if not wanted:
            continue

        for key in wanted:
            if key not in obj:
                continue
            val = obj.get(key)
            if val is None:
                continue
            if isinstance(val, str):
                v = val.strip()
                if not v:
                    continue
            else:
                try:
                    v = json.dumps(val, ensure_ascii=False)
                except Exception:
                    v = str(val)
                v = v.strip()
                if not v:
                    continue

            buffer.append(
                {
                    "raw_field_id": str(uuid.uuid4()).replace("-", ""),
                    "raw_id": raw_id,
                    "sheet_name": sheet_name,
                    "source_column": key,
                    "value_text": v,
                }
            )
            if len(buffer) >= chunk_size:
                flush()

    flush()
    db.session.commit()

    return {
        "processed_rows": processed_rows,
        "inserted_or_updated": inserted,
        "sheets": len(sheets),
        "keys_total": sum(len(v) for v in mapping.values()),
    }
