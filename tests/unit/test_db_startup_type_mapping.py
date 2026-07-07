import pytest

from app.utils.db_startup import _is_safe_pg_type, _normalize_pg_column_type, _quote_pg_identifier


def test_normalize_pg_column_type_boolean_default_zero() -> None:
    assert _normalize_pg_column_type("BOOLEAN DEFAULT 0") == "BOOLEAN DEFAULT FALSE"


def test_normalize_pg_column_type_boolean_default_one() -> None:
    assert _normalize_pg_column_type("BOOLEAN DEFAULT 1") == "BOOLEAN DEFAULT TRUE"


def test_normalize_pg_column_type_datetime() -> None:
    assert _normalize_pg_column_type("DATETIME") == "TIMESTAMP"
    assert _normalize_pg_column_type("DATETIME NOT NULL") == "TIMESTAMP NOT NULL"


def test_normalize_pg_column_type_real() -> None:
    assert _normalize_pg_column_type("REAL") == "DOUBLE PRECISION"
    assert _normalize_pg_column_type("REAL NOT NULL") == "DOUBLE PRECISION NOT NULL"


def test_normalize_pg_column_type_unknown_passthrough() -> None:
    assert _normalize_pg_column_type("VARCHAR(30)") == "VARCHAR(30)"


def test_pg_type_whitelist_accepts_expected_runtime_column_types() -> None:
    assert _is_safe_pg_type("TEXT")
    assert _is_safe_pg_type("VARCHAR(120)")
    assert _is_safe_pg_type("BOOLEAN DEFAULT TRUE")
    assert _is_safe_pg_type("TIMESTAMP NOT NULL")
    assert _is_safe_pg_type("TEXT DEFAULT '[]'")


def test_pg_type_whitelist_rejects_injected_ddl() -> None:
    assert not _is_safe_pg_type("TEXT; DROP TABLE users")
    assert not _is_safe_pg_type("VARCHAR(20)); DROP TABLE users; --")
    assert not _is_safe_pg_type("TEXT DEFAULT now()")


def test_pg_identifier_quote_rejects_unsafe_identifiers() -> None:
    assert _quote_pg_identifier("notification_queue") == '"notification_queue"'
    with pytest.raises(ValueError):
        _quote_pg_identifier('users"; DROP TABLE users; --')
    with pytest.raises(ValueError):
        _quote_pg_identifier("123bad")
