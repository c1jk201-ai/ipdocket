from __future__ import annotations

from app.extensions import db

EXCLUDED_TABLES = {
    # Legacy invoice tables (handled separately / deprecated).
    "invoices",
    "invoice_items",
    "payments",
    # View-backed model (must not be created as a physical table).
    "v_matter_overview",
}


def create_all_without_legacy_invoices(engine=None) -> None:
    target_engine = engine or db.engine
    tables = [t for t in db.metadata.sorted_tables if t.name not in EXCLUDED_TABLES]
    db.metadata.create_all(target_engine, tables=tables)
