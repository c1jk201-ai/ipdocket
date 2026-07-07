def _reset_invoice_raw_tables(conn, tables):
    try:
        conn.execute("DELETE FROM accounting_periods")
    except Exception:
        conn.rollback()
    for t in tables:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            # Keep tests resilient across slightly different invoice-module schemas.
            conn.rollback()
            continue
    conn.commit()


def test_vat_report_uses_billing_status_and_period_compat(admin_client, app, monkeypatch):
    """
    Regression coverage:
    - VAT report should consider split status column (billing_status), not legacy `status`.
    - Legacy query params should still work:
      - period=2 (numeric quarter) -> Q2
      - status_scope=completed -> issued
    """
    monkeypatch.setitem(app.config, "INVOICEAPP_DISABLE_ACCOUNTING_FEATURES", False)

    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()
        _reset_invoice_raw_tables(
            conn,
            [
                "expenses",
                "invoices",
                "clients",
                "business_profile",
            ],
        )

        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute("INSERT INTO clients (id, name) VALUES (1, 'Client A')")
        # Key: status='open' (legacy), billing_status='tax_issued' (actual billing state)
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date, due_date,
                status, billing_status, payment_status,
                subtotal, tax, total, currency, vat_rate
            ) VALUES (
                1, 1, 1, 'INV-0001', '2024-06-01', '2024-06-30',
                'open', 'tax_issued', 'unpaid',
                1000, 100, 1100, 'USD', 10.0
            )
            """
        )
        conn.commit()
        conn.close()

    res = admin_client.get(
        "/accounting/invoice-system/reports/vatNewyear=2024&period=2&basis=issue_date&status_scope=completed&currency=USD"
    )
    assert res.status_code == 200
    html = res.data.decode("utf-8")
    assert "Sales tax report" in html
    assert "2024 2quarter" in html
    assert "1,000.00 USD" in html
    assert "100.00 USD" in html
    assert "1,100.00 USD" in html

    # Clean up invoice-module tables to avoid cross-test contamination.
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
                "business_profile",
            ],
        )
        conn.close()


def test_ledger_summary_includes_accounts_without_entries_when_bp_filter(
    admin_client, app, monkeypatch
):
    """
    Regression coverage:
    When filtering by business_profile_id, the summary view should still list accounts
    with no entries (LEFT JOIN must remain effective).
    """
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
            ],
        )

        # Ensure FK target exists (init_db may not always seed this row).
        conn.execute(
            "INSERT OR IGNORE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 0.0, 1)"
        )

        conn.execute(
            """
            INSERT INTO journal_entries (
                id, entry_date, memo, business_profile_id, source_type, approved, posted
            ) VALUES (1, '2024-06-01', 't', 1, 'manual', 1, 1)
            """
        )
        conn.execute(
            "INSERT INTO journal_lines (entry_id, account_id, debit, credit, currency, description) VALUES (1, 1, 100, 0, 'USD', 'd')"
        )
        conn.execute(
            "INSERT INTO journal_lines (entry_id, account_id, debit, credit, currency, description) VALUES (1, 6, 0, 100, 'USD', 'c')"
        )
        conn.commit()
        conn.close()

    res = admin_client.get(
        "/accounting/invoice-system/ledgerNewstart_date=2024-06-01&end_date=2024-06-30&business_profile_id=1"
    )
    assert res.status_code == 200
    html = res.data.decode("utf-8")
    # Account with no entries should still be visible in the summary table.
    assert "1120" in html

    # Clean up invoice-module tables to avoid cross-test contamination.
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
                "business_profile",
            ],
        )
        conn.close()
