import re
from pathlib import Path

DDL_PATTERN = re.compile(r"\b(create|alter|drop)\s+(table|index)\b", re.IGNORECASE | re.MULTILINE)

EXCLUDE_FILES = {
    Path("app/utils/db_startup.py"),
    Path("app/blueprints/billing_invoices/db.py"),
}

EXCLUDE_DIRS = {"migrations", "scripts", ".venv", "__pycache__"}


def _iter_py_files(root: Path) -> list[Path]:
    files = []
    for path in (root / "app").rglob("*.py"):
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        rel = path.relative_to(root)
        # billing_invoices DB helpers intentionally contain DDL for the legacy invoice schema
        # (SQLite/Postgres-compat bootstrap/migration logic).
        if rel.parent == Path("app/blueprints/billing_invoices") and rel.name.startswith("db_"):
            continue
        if rel in EXCLUDE_FILES:
            continue
        files.append(path)
    return files


def test_no_runtime_ddl_in_app_paths():
    root = Path(__file__).resolve().parents[2]
    offenders = []
    for path in _iter_py_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")
        if DDL_PATTERN.search(text):
            offenders.append(str(path.relative_to(root)))
    assert not offenders, f"Runtime DDL patterns detected: {offenders}"


def test_legacy_billing_add_columns_use_idempotent_helper():
    root = Path(__file__).resolve().parents[2]
    migration_path = root / "legacy_billing_schema" / "db_migrations.py"
    text = migration_path.read_text(encoding="utf-8")
    raw_add_column = re.compile(r"ALTER\s+TABLE\s+[^\"']+\s+ADD\s+COLUMN", re.IGNORECASE)

    assert not raw_add_column.findall(text)
