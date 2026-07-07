def _seed_delete_invoice_data(app, *, invoice_ids: list[int]) -> None:
    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()
        conn.execute(
            "INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute("INSERT INTO clients (id, name) VALUES (1, 'Client A')")
        conn.execute(
            """
            INSERT INTO client_deposit_ledger (
                business_profile_id, client_id, currency, amount_minor, entry_type, memo
            ) VALUES (1, 1, 'USD', 100000, 'topup', 'seed')
            """
        )

        for invoice_id in invoice_ids:
            conn.execute(
                """
                INSERT INTO invoices (
                    id,
                    client_id,
                    business_profile_id,
                    number,
                    issue_date,
                    due_date,
                    status,
                    billing_status,
                    payment_status,
                    payment_verified,
                    currency,
                    subtotal,
                    tax,
                    total,
                    subtotal_minor,
                    tax_minor,
                    total_minor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invoice_id,
                    1,
                    1,
                    f"INV-DELETE-{invoice_id:04d}",
                    "2026-03-01",
                    "2026-04-01",
                    "payment_pending",
                    "sent",
                    "pending",
                    0,
                    "USD",
                    10000,
                    1000,
                    11000,
                    10000,
                    1000,
                    11000,
                ),
            )

        conn.execute(
            """
            INSERT INTO client_deposit_ledger (
                business_profile_id, client_id, currency, amount_minor, entry_type, memo, related_invoice_id
            ) VALUES (1, 1, 'USD', -3300, 'apply', 'seed-apply', 1)
            """
        )
        conn.commit()
        conn.close()


def test_delete_invoice_cancels_partial_deposit_and_removes_invoice(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_delete_invoice_data(app, invoice_ids=[1])

    resp = admin_client.post(
        "/accounting/invoice-system/invoices/1/delete",
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/accounting/invoice-system/invoices")

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        remaining = conn.execute("SELECT COUNT(*) AS n FROM invoices WHERE id=1").fetchone()
        assert int(remaining["n"] or 0) == 0

        balance = conn.execute(
            """
            SELECT COALESCE(SUM(amount_minor), 0) AS s
            FROM client_deposit_ledger
            WHERE client_id=1 AND currency='USD'
            """
        ).fetchone()
        assert int(balance["s"] or 0) == 100000

        cancels = conn.execute(
            "SELECT COUNT(*) AS n FROM client_deposit_ledger WHERE entry_type='cancel_apply'"
        ).fetchone()
        assert int(cancels["n"] or 0) == 1
        conn.close()


def test_bulk_delete_removes_multiple_invoices(admin_client, app, clean_legacy_invoice_db):
    _seed_delete_invoice_data(app, invoice_ids=[1, 2])

    resp = admin_client.post(
        "/accounting/invoice-system/invoices/bulk_delete",
        data={"invoice_ids[]": ["1", "2"]},
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/accounting/invoice-system/invoices")

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        remaining = conn.execute("SELECT COUNT(*) AS n FROM invoices").fetchone()
        assert int(remaining["n"] or 0) == 0

        balance = conn.execute(
            """
            SELECT COALESCE(SUM(amount_minor), 0) AS s
            FROM client_deposit_ledger
            WHERE client_id=1 AND currency='USD'
            """
        ).fetchone()
        assert int(balance["s"] or 0) == 100000
        conn.close()
