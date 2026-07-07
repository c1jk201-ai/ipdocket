def _reset_numbering_tables(conn):
    for t in [
        "invoice_number_counters",
        "line_items",
        "invoices",
        "external_invoice_case_map",
        "clients",
        "business_profile",
    ]:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            continue
    conn.commit()


def _seed_minimum_profiles_and_client(conn):
    conn.execute(
        """
        INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no)
        VALUES (1, 'BP1', 'USD', 10.0, 99)
        """
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no)
        VALUES (2, 'BP2', 'USD', 0.0, 777)
        """
    )
    conn.execute("INSERT OR REPLACE INTO clients (id, name) VALUES (1, 'Client A')")
    conn.commit()


def test_next_invoice_number_uses_global_daily_sequence(app):
    from app.blueprints.billing_invoices.db import get_db, init_db, next_invoice_number

    with app.app_context():
        init_db()
        conn = get_db()
        _reset_numbering_tables(conn)
        _seed_minimum_profiles_and_client(conn)

        prefix = "INV-20260228-"
        n1 = next_invoice_number(conn, 1, prefix)
        n2 = next_invoice_number(conn, 2, prefix)

        assert n1 == "INV-20260228-0001"
        assert n2 == "INV-20260228-0002"

        row = conn.execute(
            "SELECT last_no FROM invoice_number_counters WHERE date_key=?",
            ("20260228",),
        ).fetchone()
        assert int(row[0] or 0) == 2

        # Legacy per-business counter should no longer be used by numbering.
        bp1 = conn.execute("SELECT next_invoice_no FROM business_profile WHERE id=1").fetchone()
        bp2 = conn.execute("SELECT next_invoice_no FROM business_profile WHERE id=2").fetchone()
        assert int(bp1[0] or 0) == 99
        assert int(bp2[0] or 0) == 777

        _reset_numbering_tables(conn)
        conn.close()


def test_next_invoice_number_seeds_from_existing_numbers_of_the_day(app):
    from app.blueprints.billing_invoices.db import get_db, init_db, next_invoice_number

    with app.app_context():
        init_db()
        conn = get_db()
        _reset_numbering_tables(conn)
        _seed_minimum_profiles_and_client(conn)

        # Existing numbers can come from multiple business profiles.
        conn.execute(
            "INSERT INTO invoices (client_id, business_profile_id, number) VALUES (?, ?, ?)",
            (1, 1, "INV-20260228-0003"),
        )
        conn.execute(
            "INSERT INTO invoices (client_id, business_profile_id, number) VALUES (?, ?, ?)",
            (1, 2, "INV-20260228-0007"),
        )
        conn.execute(
            "INSERT INTO invoices (client_id, business_profile_id, number) VALUES (?, ?, ?)",
            (1, 2, "INV-20260227-0012"),
        )
        conn.commit()

        number = next_invoice_number(conn, 1, "INV-20260228-")
        assert number == "INV-20260228-0008"

        next_day_number = next_invoice_number(conn, 1, "INV-20260301-")
        assert next_day_number == "INV-20260301-0001"

        _reset_numbering_tables(conn)
        conn.close()
