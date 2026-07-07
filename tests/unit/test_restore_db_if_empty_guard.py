from __future__ import annotations


def test_restore_db_if_empty_uses_table_count_guard() -> None:
    """
    Regression guard:
    - The db_restore helper must not decide "DB is empty" based on invoice tables,
      because that can trigger a full restore on every deploy and wipe admin changes.
    """
    script = open("scripts/restore_db_if_empty.sh", encoding="utf-8").read()

    assert "information_schema.tables" in script
    assert "DB_RESTORE_FORCE" in script
