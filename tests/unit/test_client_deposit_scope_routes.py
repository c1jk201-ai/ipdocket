def _seed_client_deposit_scope_fixture(app):
    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()
        conn.execute(
            "INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute(
            "INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (2, 'BP2', 'USD', 10.0, 1)"
        )
        conn.execute("INSERT INTO clients (id, name) VALUES (1, 'Client A')")
        conn.commit()
        conn.close()


def test_client_deposit_topup_defaults_to_global_scope_when_scope_missing(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_client_deposit_scope_fixture(app)

    resp = admin_client.post(
        "/accounting/invoice-system/clients/1/deposit/topup",
        data={"currency": "USD", "amount": "10000", "memo": "global-default"},
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert "business_profile_id=global" in (resp.headers.get("Location") or "")

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        row = conn.execute(
            """
            SELECT business_profile_id, currency, amount_minor, entry_type, memo
            FROM client_deposit_ledger
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert row is not None
        assert row["business_profile_id"] is None
        assert row["currency"] == "USD"
        assert int(row["amount_minor"] or 0) == 1000000
        assert row["entry_type"] == "topup"
        assert row["memo"] == "global-default"
        conn.close()


def test_client_deposit_topup_allows_explicit_business_profile_scope(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_client_deposit_scope_fixture(app)

    resp = admin_client.post(
        "/accounting/invoice-system/clients/1/deposit/topup",
        data={
            "business_profile_id": "2",
            "currency": "USD",
            "amount": "25000",
            "memo": "bp-specific",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert "business_profile_id=2" in (resp.headers.get("Location") or "")

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        row = conn.execute(
            """
            SELECT business_profile_id, currency, amount_minor, entry_type, memo
            FROM client_deposit_ledger
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert row is not None
        assert row["business_profile_id"] == 2
        assert row["currency"] == "USD"
        assert int(row["amount_minor"] or 0) == 2500000
        assert row["entry_type"] == "topup"
        assert row["memo"] == "bp-specific"
        conn.close()


def test_client_deposit_ledger_filters_global_scope_separately(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_client_deposit_scope_fixture(app)

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        conn.execute(
            """
            INSERT INTO client_deposit_ledger (
                business_profile_id, client_id, currency, amount_minor, entry_type, memo
            ) VALUES (NULL, 1, 'USD', 10000, 'topup', 'global-only')
            """
        )
        conn.execute(
            """
            INSERT INTO client_deposit_ledger (
                business_profile_id, client_id, currency, amount_minor, entry_type, memo
            ) VALUES (2, 1, 'USD', 25000, 'topup', 'bp-only')
            """
        )
        conn.commit()
        conn.close()

    resp = admin_client.get(
        "/accounting/invoice-system/clients/1/deposit?business_profile_id=global"
    )

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert ": All()" in html
    assert "global-only" in html
    assert "bp-only" not in html
