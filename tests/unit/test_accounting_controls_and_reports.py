def _reset_invoice_raw_tables(conn, tables):
    try:
        conn.execute("DELETE FROM accounting_periods")
    except Exception:
        conn.rollback()
    for table in tables:
        try:
            conn.execute(f"DELETE FROM {table}")
        except Exception:
            conn.rollback()
            continue
    conn.commit()


def test_journal_workflow_requires_post_and_uses_reversal(admin_client, app, monkeypatch):
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
                "accounting_periods",
                "business_profile",
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute(
            """
            INSERT INTO journal_entries (
                id, entry_date, memo, business_profile_id, source_type, approved, posted
            ) VALUES (1, '2024-06-01', 'Draft JE', 1, 'manual', 0, 0)
            """
        )
        conn.execute(
            "INSERT INTO journal_lines (entry_id, account_id, debit, credit, currency, description) VALUES (1, 1, 100, 0, 'USD', 'cash')"
        )
        conn.execute(
            "INSERT INTO journal_lines (entry_id, account_id, debit, credit, currency, description) VALUES (1, 6, 0, 100, 'USD', 'sales')"
        )
        conn.commit()
        conn.close()

    res_before = admin_client.get(
        "/accounting/invoice-system/ledgerLegacyaccount_id=1&start_date=2024-06-01&end_date=2024-06-30&business_profile_id=1"
    )
    assert res_before.status_code == 200
    assert "Draft JE" not in res_before.data.decode("utf-8")

    approve_res = admin_client.post(
        "/accounting/invoice-system/ledger/journal/1/approve",
        follow_redirects=True,
    )
    assert approve_res.status_code == 200
    assert "Journal entry approved." in approve_res.data.decode("utf-8")

    post_res = admin_client.post(
        "/accounting/invoice-system/ledger/journal/1/post",
        follow_redirects=True,
    )
    assert post_res.status_code == 200
    assert "Journal entry posted. Ledger updated." in post_res.data.decode("utf-8")

    res_after = admin_client.get(
        "/accounting/invoice-system/ledgerLegacyaccount_id=1&start_date=2024-06-01&end_date=2024-06-30&business_profile_id=1"
    )
    assert res_after.status_code == 200
    assert "Draft JE" in res_after.data.decode("utf-8")

    reverse_res = admin_client.post(
        "/accounting/invoice-system/ledger/journal/1/reverse",
        data={"reversal_date": "2024-07-01", "reversal_memo": "closing correction"},
        follow_redirects=True,
    )
    assert reverse_res.status_code == 200
    html = reverse_res.data.decode("utf-8")
    assert "Journal entry reversed." in html
    assert "closing correction" in html

    with app.app_context():
        conn = get_db()
        original = conn.execute(
            "SELECT reversed, reversed_by_entry_id FROM journal_entries WHERE id = 1"
        ).fetchone()
        assert int(original[0] or 0) == 1
        reversal = conn.execute(
            """
            SELECT source_type, approved, posted, reversal_of_entry_id
              FROM journal_entries
             WHERE id = ?
            """,
            (int(original[1]),),
        ).fetchone()
        assert reversal[0] == "reversal"
        assert int(reversal[1] or 0) == 1
        assert int(reversal[2] or 0) == 1
        assert int(reversal[3] or 0) == 1
        conn.close()


def test_period_close_requires_no_unposted_entries_and_blocks_new_journals(
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
                "journal_lines",
                "journal_entries",
                "accounting_periods",
                "business_profile",
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute(
            """
            INSERT INTO journal_entries (
                id, entry_date, memo, business_profile_id, source_type, approved, posted
            ) VALUES (1, '2024-06-10', 'Pending JE', 1, 'manual', 1, 0)
            """
        )
        conn.execute(
            "INSERT INTO journal_lines (entry_id, account_id, debit, credit, currency, description) VALUES (1, 1, 100, 0, 'USD', 'cash')"
        )
        conn.execute(
            "INSERT INTO journal_lines (entry_id, account_id, debit, credit, currency, description) VALUES (1, 6, 0, 100, 'USD', 'sales')"
        )
        conn.commit()
        conn.close()

    close_fail = admin_client.post(
        "/accounting/invoice-system/reports/period-close",
        data={
            "business_profile_id": "1",
            "period_type": "monthly",
            "start_date": "2024-06-01",
            "end_date": "2024-06-30",
        },
        follow_redirects=True,
    )
    assert close_fail.status_code == 200
    assert "Cannot close period while unposted journal entries exist." in close_fail.data.decode(
        "utf-8"
    )

    with app.app_context():
        conn = get_db()
        conn.execute("UPDATE journal_entries SET posted = 1, posted_at = created_at WHERE id = 1")
        conn.commit()
        conn.close()

    close_ok = admin_client.post(
        "/accounting/invoice-system/reports/period-close",
        data={
            "business_profile_id": "1",
            "period_type": "monthly",
            "start_date": "2024-06-01",
            "end_date": "2024-06-30",
        },
        follow_redirects=True,
    )
    assert close_ok.status_code == 200
    assert "Accounting period closed." in close_ok.data.decode("utf-8")

    create_blocked = admin_client.post(
        "/accounting/invoice-system/ledger/journal/new",
        data={
            "entry_date": "2024-06-20",
            "business_profile_id": "1",
            "currency": "USD",
            "memo": "Blocked JE",
            "account_id": ["1", "6"],
            "debit": ["100", "0"],
            "credit": ["0", "100"],
            "line_description": ["cash", "sales"],
        },
        follow_redirects=True,
    )
    assert create_blocked.status_code == 200
    assert "Cannot save journal entry in a closed accounting period." in create_blocked.data.decode(
        "utf-8"
    )

    with app.app_context():
        conn = get_db()
        locked = conn.execute("SELECT locked_period FROM journal_entries WHERE id = 1").fetchone()
        assert int(locked[0] or 0) == 1
        count = conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
        assert int(count or 0) == 1
        conn.close()


def test_financial_reports_use_only_posted_entries(admin_client, app, monkeypatch):
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
                "accounting_periods",
                "business_profile",
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute(
            """
            INSERT INTO journal_entries (
                id, entry_date, memo, business_profile_id, source_type, approved, posted
            ) VALUES (1, '2024-06-01', 'Posted JE', 1, 'manual', 1, 1)
            """
        )
        conn.execute(
            """
            INSERT INTO journal_entries (
                id, entry_date, memo, business_profile_id, source_type, approved, posted
            ) VALUES (2, '2024-06-02', 'Draft JE', 1, 'manual', 0, 0)
            """
        )
        for entry_id, amount in ((1, 1000), (2, 500)):
            conn.execute(
                "INSERT INTO journal_lines (entry_id, account_id, debit, credit, currency, description) VALUES (?, 1, ?, 0, 'USD', 'cash')",
                (entry_id, amount),
            )
            conn.execute(
                "INSERT INTO journal_lines (entry_id, account_id, debit, credit, currency, description) VALUES (?, 6, 0, ?, 'USD', 'sales')",
                (entry_id, amount),
            )
        conn.commit()
        conn.close()

    trial = admin_client.get(
        "/accounting/invoice-system/reports/trial-balanceNewstart_date=2024-06-01&end_date=2024-06-30&business_profile_id=1&currency=USD"
    )
    assert trial.status_code == 200
    trial_html = trial.data.decode("utf-8")
    assert "Trial Balance" in trial_html
    assert "1,000.00 USD" in trial_html
    assert "1,500.00 USD" not in trial_html

    income = admin_client.get(
        "/accounting/invoice-system/reports/income-statementNewstart_date=2024-06-01&end_date=2024-06-30&business_profile_id=1&currency=USD"
    )
    assert income.status_code == 200
    income_html = income.data.decode("utf-8")
    assert "Income Statement" in income_html
    assert "1,000.00 USD" in income_html
    assert "1,500.00 USD" not in income_html

    balance = admin_client.get(
        "/accounting/invoice-system/reports/balance-sheetNewas_of_date=2024-06-30&business_profile_id=1&currency=USD"
    )
    assert balance.status_code == 200
    balance_html = balance.data.decode("utf-8")
    assert "Balance Sheet" in balance_html
    assert "1,000.00 USD" in balance_html
    assert "1,500.00 USD" not in balance_html


def test_business_menu_includes_erp_accounting_links(admin_client, app, monkeypatch):
    monkeypatch.setitem(app.config, "INVOICEAPP_DISABLE_ACCOUNTING_FEATURES", False)

    from app.blueprints.billing_invoices.db import init_db

    with app.app_context():
        init_db()

    res = admin_client.get("/business/")
    assert res.status_code == 200
    html = res.data.decode("utf-8")
    assert "Financial reports" in html
    assert "General ledger" in html
    assert "Journal entries" in html
    assert 'href="/business/accounting/reports' in html
    assert 'href="/business/accounting/ledger' in html


def test_business_menu_tolerates_missing_accounting_endpoints(admin_client, app, monkeypatch):
    """Older/stale runtimes may not have the business accounting endpoints registered."""
    monkeypatch.setitem(app.config, "INVOICEAPP_DISABLE_ACCOUNTING_FEATURES", False)
    from app.blueprints.billing_invoices.db import init_db

    with app.app_context():
        init_db()

    for endpoint in (
        "business.accounting_reports_home",
        "business.accounting_general_ledger",
        "business.accounting_journal",
        "business.accounting_trial_balance_report",
        "business.accounting_income_statement_report",
        "business.accounting_balance_sheet_report",
        "business.accounting_period_close",
        "business.accounting_vat_report",
    ):
        monkeypatch.delitem(app.view_functions, endpoint, raising=False)

    res = admin_client.get("/business/")

    assert res.status_code == 200
    html = res.data.decode("utf-8")
    assert "Invoice dashboard" in html
    assert "Financial reports" not in html
