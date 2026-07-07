from __future__ import annotations

import re
from typing import Iterable

from flask import current_app
from app.extensions import db
from app.utils.policy_sql import policy_text as text

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WORKFLOW_KNOWN_CHILDREN: tuple[tuple[str, str], ...] = (
    ("workflow_checklist_item", "workflow_id"),
    ("workflow_reminder_sent", "workflow_id"),
)


def _base_table_name(table_name: str) -> str:
    raw = (table_name or "").strip()
    if not raw:
        return ""
    base = raw.split(".")[-1]
    return base.strip('"').lower()


def _quote_ident_chain(name: str) -> str:
    raw = (name or "").strip()
    if not raw:
        raise ValueError("empty SQL identifier")

    parts = [p.strip().strip('"') for p in raw.split(".") if p.strip()]
    if not parts:
        raise ValueError(f"invalid SQL identifier: {name!r}")

    quoted: list[str] = []
    for part in parts:
        if not _IDENT_RE.match(part):
            raise ValueError(f"unsafe SQL identifier: {name!r}")
        quoted.append(f'"{part}"')
    return ".".join(quoted)


def _list_single_column_fk_children(parent_table: str) -> list[tuple[str, str]]:
    # This helper is for production Postgres.
    # In sqlite test env we just skip dynamic FK discovery.
    dialect = (db.engine.dialect.name or "").lower()
    if not dialect.startswith("postgres"):
        return []

    rows = (
        db.session.execute(
            text(
                """
                SELECT
                    c.conrelid::regclass::text AS child_table,
                    a.attname AS fk_col
                FROM pg_constraint c
                JOIN pg_attribute a
                  ON a.attrelid = c.conrelid
                 AND a.attnum = c.conkey[1]
                WHERE c.contype = 'f'
                  AND c.confrelid = to_regclass(:parent_table)
                  AND cardinality(c.conkey) = 1
                  AND cardinality(c.confkey) = 1
                ORDER BY c.conrelid::regclass::text, c.conname
                """
            ),
            {"parent_table": parent_table},
        )
        .mappings()
        .all()
    )
    out: list[tuple[str, str]] = []
    for row in rows:
        child = (row.get("child_table") or "").strip()
        fk_col = (row.get("fk_col") or "").strip()
        if child and fk_col:
            out.append((child, fk_col))
    return out


def delete_workflow_fk_children_for_matter(matter_id: str) -> None:
    mid = (matter_id or "").strip()
    if not mid:
        return

    for child_table, fk_col in _list_single_column_fk_children("workflows"):
        # Parent table itself is deleted later.
        if _base_table_name(child_table) == "workflows":
            continue
        try:
            q_child = _quote_ident_chain(child_table)
            q_col = _quote_ident_chain(fk_col)
            db.session.execute(
                text(
                    f"""
                    DELETE FROM {q_child}
                    WHERE {q_col} IN (
                        SELECT id FROM workflows WHERE case_id = :mid
                    )
                    """
                ),
                {"mid": mid},
            )
        except Exception:
            current_app.logger.exception(
                "Workflow FK child cleanup failed (matter_id=%s, child=%s, fk_col=%s)",
                mid,
                child_table,
                fk_col,
            )
            raise


def delete_workflow_fk_children(workflow_id: int | str) -> None:
    raw = str(workflow_id or "").strip()
    if not raw:
        return
    try:
        wf_id = int(raw)
    except Exception:
        return
    if wf_id <= 0:
        return

    cleaned_tables: set[str] = set()

    for child_table, fk_col in _list_single_column_fk_children("workflows"):
        base = _base_table_name(child_table)
        # Parent table itself is deleted later.
        if base == "workflows":
            continue
        try:
            q_child = _quote_ident_chain(child_table)
            q_col = _quote_ident_chain(fk_col)
            db.session.execute(
                text(
                    f"""
                    DELETE FROM {q_child}
                    WHERE {q_col} = :wf_id
                    """
                ),
                {"wf_id": wf_id},
            )
            cleaned_tables.add(base)
        except Exception:
            current_app.logger.exception(
                "Workflow FK child cleanup failed (workflow_id=%s, child=%s, fk_col=%s)",
                wf_id,
                child_table,
                fk_col,
            )
            raise

    # sqlite/unit tests skip dynamic FK discovery; keep known child cleanup explicit.
    for child_table, fk_col in _WORKFLOW_KNOWN_CHILDREN:
        if child_table in cleaned_tables:
            continue
        try:
            q_child = _quote_ident_chain(child_table)
            q_col = _quote_ident_chain(fk_col)
            db.session.execute(
                text(
                    f"""
                    DELETE FROM {q_child}
                    WHERE {q_col} = :wf_id
                    """
                ),
                {"wf_id": wf_id},
            )
        except Exception:
            current_app.logger.exception(
                "Workflow FK child cleanup failed (workflow_id=%s, child=%s, fk_col=%s)",
                wf_id,
                child_table,
                fk_col,
            )
            raise


def delete_matter_fk_children(
    matter_id: str, *, exclude_tables: Iterable[str] | None = None
) -> None:
    mid = (matter_id or "").strip()
    if not mid:
        return

    excluded = {_base_table_name(x) for x in (exclude_tables or []) if str(x).strip()}
    excluded.add("matter")

    for child_table, fk_col in _list_single_column_fk_children("matter"):
        if _base_table_name(child_table) in excluded:
            continue
        try:
            q_child = _quote_ident_chain(child_table)
            q_col = _quote_ident_chain(fk_col)
            db.session.execute(
                text(
                    f"""
                    DELETE FROM {q_child}
                    WHERE {q_col} = :mid
                    """
                ),
                {"mid": mid},
            )
        except Exception:
            current_app.logger.exception(
                "Matter FK child cleanup failed (matter_id=%s, child=%s, fk_col=%s)",
                mid,
                child_table,
                fk_col,
            )
            raise


