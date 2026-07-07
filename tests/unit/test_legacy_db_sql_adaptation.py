import app as _app_package  # noqa: F401
from flask import Flask
from legacy_billing_schema.db_core import _adapt_sql


def _adapt_for_postgres(sql: str) -> str:
    app = Flask(__name__)
    app.config["INVOICEAPP_TABLE_PREFIX"] = ""
    with app.app_context():
        return _adapt_sql(sql, "postgresql")


def test_postgres_rewrites_insert_or_ignore_to_on_conflict_do_nothing():
    sql = """
    INSERT OR IGNORE INTO accounts (id, code, name)
    VALUES (?, ?, ?);
    """

    adapted = _adapt_for_postgres(sql)

    assert "INSERT OR IGNORE" not in adapted.upper()
    assert "INSERT INTO accounts" in adapted
    assert "VALUES (%s, %s, %s)" in adapted
    assert adapted.rstrip().endswith("ON CONFLICT DO NOTHING")


def test_postgres_rewrites_insert_or_replace_to_upsert_by_id():
    sql = (
        "INSERT OR REPLACE INTO business_profile "
        "(id, name, currency, vat_rate) VALUES (?, ?, ?, ?)"
    )

    adapted = _adapt_for_postgres(sql)

    assert "INSERT OR REPLACE" not in adapted.upper()
    assert "INSERT INTO business_profile" in adapted
    assert "VALUES (%s, %s, %s, %s)" in adapted
    assert "ON CONFLICT (id) DO UPDATE SET" in adapted
    assert "name = EXCLUDED.name" in adapted
    assert "currency = EXCLUDED.currency" in adapted
    assert "vat_rate = EXCLUDED.vat_rate" in adapted
