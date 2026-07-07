def _reset_invoice_raw_tables(conn, tables):
    try:
        conn.execute("DELETE FROM accounting_periods")
    except Exception:
        conn.rollback()
    for t in tables:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            conn.rollback()
            continue
    conn.commit()


def test_expenses_currency_filter_and_multi_currency_totals(admin_client, app, monkeypatch):
    monkeypatch.setitem(app.config, "INVOICEAPP_DISABLE_ACCOUNTING_FEATURES", False)

    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()
        _reset_invoice_raw_tables(
            conn,
            [
                "journal_lines",
                "journal_entries",
                "expenses",
                "client_deposit_ledger",
                "line_items",
                "invoices",
                "external_invoice_case_map",
                "clients",
                "expense_categories",
                "business_profile",
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'USD BP', 'USD', 10.0, 1)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (2, 'EUR BP', 'EUR', 10.0, 1)"
        )
        conn.execute(
            """
            INSERT INTO expenses (
                business_profile_id, expense_date, vendor_name, currency, net_amount, vat_amount, total_amount
            ) VALUES (1, '2025-01-10', 'USD Vendor', 'USD', 100, 10, 110)
            """
        )
        conn.execute(
            """
            INSERT INTO expenses (
                business_profile_id, expense_date, vendor_name, currency, net_amount, vat_amount, total_amount
            ) VALUES (2, '2025-01-11', 'EUR Vendor', 'EUR', 20, 2, 22)
            """
        )
        conn.commit()
        conn.close()

    res_usd = admin_client.get("/accounting/invoice-system/expensesNewcurrency=USD")
    assert res_usd.status_code == 200
    html_usd = res_usd.data.decode("utf-8")
    assert "USD Vendor" in html_usd
    assert "EUR Vendor" not in html_usd
    assert "100.00 USD" in html_usd
    assert "110.00 USD" in html_usd

    res_all = admin_client.get("/accounting/invoice-system/expensesNewcurrency=ALL")
    assert res_all.status_code == 200
    html_all = res_all.data.decode("utf-8")
    assert "USD Vendor" in html_all
    assert "EUR Vendor" in html_all
    assert "100.00 USD" in html_all
    assert "20.00 EUR" in html_all

    with app.app_context():
        conn = get_db()
        _reset_invoice_raw_tables(
            conn,
            [
                "journal_lines",
                "journal_entries",
                "expenses",
                "client_deposit_ledger",
                "line_items",
                "invoices",
                "external_invoice_case_map",
                "clients",
                "expense_categories",
                "business_profile",
            ],
        )
        conn.close()


def test_invoice_create_rejects_invalid_settlement_percent_sum(admin_client, app, monkeypatch):
    monkeypatch.setitem(app.config, "INVOICEAPP_DISABLE_ACCOUNTING_FEATURES", False)

    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()
        _reset_invoice_raw_tables(
            conn,
            [
                "line_items",
                "invoices",
                "external_invoice_case_map",
                "clients",
                "business_profile",
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (2, 'BP2', 'USD', 10.0, 1)"
        )
        conn.execute("INSERT OR REPLACE INTO clients (id, name) VALUES (1, 'Client A')")
        conn.commit()
        conn.close()

    res = admin_client.post(
        "/accounting/invoice-system/invoices/new",
        data={
            "business_profile_id": "1",
            "client_id": "1",
            "issue_date": "2025-01-01",
            "due_date": "2025-02-01",
            "status": "draft",
            "invoice_language": "en",
            "description[]": ["Service fee"],
            "qty[]": ["1"],
            "unit_price[]": ["1000"],
            "settle_bp_id[]": ["1", "2"],
            "settle_percent[]": ["60", "30"],
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    html = res.data.decode("utf-8")
    assert "Settlement Total 100% . (Current 90%)" in html

    with app.app_context():
        conn = get_db()
        cnt = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        assert int(cnt or 0) == 0
        _reset_invoice_raw_tables(
            conn,
            [
                "line_items",
                "invoices",
                "external_invoice_case_map",
                "clients",
                "business_profile",
            ],
        )
        conn.close()


def test_invoice_create_ignores_default_same_profile_settlement_split(
    admin_client, app, monkeypatch
):
    monkeypatch.setitem(app.config, "INVOICEAPP_DISABLE_ACCOUNTING_FEATURES", False)

    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()
        _reset_invoice_raw_tables(
            conn,
            [
                "line_items",
                "invoices",
                "external_invoice_case_map",
                "clients",
                "business_profile",
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute("INSERT OR REPLACE INTO clients (id, name) VALUES (1, 'Client A')")
        conn.commit()
        conn.close()

    res = admin_client.post(
        "/accounting/invoice-system/invoices/new",
        data={
            "business_profile_id": "1",
            "client_id": "1",
            "issue_date": "2025-01-01",
            "due_date": "2025-02-01",
            "status": "draft",
            "invoice_language": "en",
            "description[]": ["Service fee"],
            "qty[]": ["1"],
            "unit_price[]": ["1000"],
            "settle_bp_id[]": ["1"],
            "settle_percent[]": ["100"],
        },
        follow_redirects=False,
    )
    assert res.status_code == 302

    with app.app_context():
        conn = get_db()
        row = conn.execute(
            "SELECT id, settlement_meta FROM invoices ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["settlement_meta"] is None
        invoice_id = int(row["id"])
        conn.close()

    view = admin_client.get(f"/accounting/invoice-system/invoices/{invoice_id}")
    assert view.status_code == 200
    html = view.data.decode("utf-8")
    assert "Settlement business profile" not in html
    assert "Internal Settlement Done" not in html

    with app.app_context():
        conn = get_db()
        _reset_invoice_raw_tables(
            conn,
            [
                "line_items",
                "invoices",
                "external_invoice_case_map",
                "clients",
                "business_profile",
            ],
        )
        conn.close()


def test_invoice_create_saves_new_client_name_en_to_crm_extra(
    admin_client, app, monkeypatch, clean_legacy_invoice_db
):
    monkeypatch.setitem(app.config, "INVOICEAPP_DISABLE_ACCOUNTING_FEATURES", False)

    from app.blueprints.billing_invoices.db import get_db, init_db, safe_json_parse

    with app.app_context():
        init_db()
        conn = get_db()
        _reset_invoice_raw_tables(
            conn,
            [
                "line_items",
                "invoices",
                "external_invoice_case_map",
                "clients",
                "business_profile",
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'USD BP', 'USD', 0.0, 1)"
        )
        conn.commit()
        conn.close()

    response = admin_client.post(
        "/accounting/invoice-system/invoices/new",
        data={
            "business_profile_id": "1",
            "client_id": "",
            "new_client_name": "Kancelaria Adwokacko-Patentowa",
            "new_client_name_en": "Kancelaria Adwokacko-Patentowa",
            "new_client_email": "info@ppklegal.pl",
            "new_client_manager": "Maria Przybylska-Karczemska",
            "issue_date": "2026-03-25",
            "due_date": "2026-04-25",
            "status": "draft",
            "invoice_language": "en",
            "description[]": ["Service fee"],
            "qty[]": ["1"],
            "unit_price[]": ["1000"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 302

    with app.app_context():
        conn = get_db()
        row = conn.execute(
            "SELECT name, email, manager, extra FROM clients WHERE name=?",
            ("Kancelaria Adwokacko-Patentowa",),
        ).fetchone()
        assert row is not None
        extra = safe_json_parse(row["extra"], {}) or {}
        assert row["email"] == "info@ppklegal.pl"
        assert row["manager"] == "Maria Przybylska-Karczemska"
        assert extra.get("name_en") == "Kancelaria Adwokacko-Patentowa"
        conn.close()


def test_invoice_list_uses_dynamic_total_for_amount_filter_and_sort(admin_client, app, monkeypatch):
    monkeypatch.setitem(app.config, "INVOICEAPP_DISABLE_ACCOUNTING_FEATURES", False)

    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()
        _reset_invoice_raw_tables(
            conn,
            [
                "line_items",
                "invoices",
                "external_invoice_case_map",
                "clients",
                "business_profile",
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute("INSERT OR REPLACE INTO clients (id, name) VALUES (1, 'Client A')")
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date, due_date,
                status, billing_status, payment_status, subtotal, tax, total, currency, vat_rate
            ) VALUES (
                1, 1, 1, 'INV-A', '2025-01-01', '2025-01-31',
                'sent', 'sent', 'unpaid', 0, 0, 10, 'USD', 10.0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date, due_date,
                status, billing_status, payment_status, subtotal, tax, total, currency, vat_rate
            ) VALUES (
                2, 1, 1, 'INV-B', '2025-01-02', '2025-02-01',
                'sent', 'sent', 'unpaid', 0, 0, 1000, 'USD', 10.0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO line_items (
                invoice_id, description, qty, unit_price, item_type, discount, is_taxable, is_estimated
            ) VALUES (1, 'A service', 1, 100, 'service', 0, 1, 0)
            """
        )
        conn.execute(
            """
            INSERT INTO line_items (
                invoice_id, description, qty, unit_price, item_type, discount, is_taxable, is_estimated
            ) VALUES (2, 'B service', 1, 20, 'service', 0, 1, 0)
            """
        )
        conn.commit()
        conn.close()

    res_filtered = admin_client.get(
        "/accounting/invoice-system/invoicesNewsort=amount_desc&min_amount=100"
    )
    assert res_filtered.status_code == 200
    html_filtered = res_filtered.data.decode("utf-8")
    assert "INV-A" in html_filtered
    assert "INV-B" not in html_filtered

    res_sorted = admin_client.get("/accounting/invoice-system/invoicesNewsort=amount_desc")
    assert res_sorted.status_code == 200
    html_sorted = res_sorted.data.decode("utf-8")
    assert "INV-A" in html_sorted
    assert "INV-B" in html_sorted
    assert html_sorted.index("INV-A") < html_sorted.index("INV-B")

    with app.app_context():
        conn = get_db()
        _reset_invoice_raw_tables(
            conn,
            [
                "line_items",
                "invoices",
                "external_invoice_case_map",
                "clients",
                "business_profile",
            ],
        )
        conn.close()
