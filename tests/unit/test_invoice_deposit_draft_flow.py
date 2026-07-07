import re


def _seed_invoice_db_minimal(app):
    """Create minimal invoice-module data for deposit tests."""
    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()

        # Clean slate (invoice-module tables live outside SQLAlchemy metadata).
        for tbl in (
            "client_deposit_ledger",
            "line_items",
            "invoices",
            "external_invoice_case_map",
            "clients",
            "business_profile",
        ):
            try:
                conn.execute(f"DELETE FROM {tbl}")
            except Exception:
                # Some tables may not exist on older schemas; keep going.
                continue

        conn.execute(
            "INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute("INSERT INTO clients (id, name) VALUES (1, 'Client A')")

        # Deposit topup: 1,100 USD in cents.
        conn.execute(
            """
            INSERT INTO client_deposit_ledger (
                business_profile_id, client_id, currency, amount_minor, entry_type, memo
            ) VALUES (1, 1, 'USD', 110000, 'topup', 'seed')
            """
        )

        conn.commit()
        conn.close()


def _seed_invoice_db_minimal_global_deposit(app):
    """Create minimal invoice-module data with a global (business_profile_id NULL) deposit."""
    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()

        for tbl in (
            "client_deposit_ledger",
            "line_items",
            "invoices",
            "external_invoice_case_map",
            "clients",
            "business_profile",
        ):
            try:
                conn.execute(f"DELETE FROM {tbl}")
            except Exception:
                continue

        conn.execute(
            "INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute("INSERT INTO clients (id, name) VALUES (1, 'Client A')")

        # Global deposit topup (bp=NULL): 100,000 USD.
        conn.execute(
            """
            INSERT INTO client_deposit_ledger (
                business_profile_id, client_id, currency, amount_minor, entry_type, memo
            ) VALUES (NULL, 1, 'USD', 100000, 'topup', 'seed-global')
            """
        )

        conn.commit()
        conn.close()


def _seed_invoice_db_minimal_mixed_deposit(app):
    """Create minimal invoice-module data with profile-specific and global deposits."""
    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()

        for tbl in (
            "client_deposit_ledger",
            "line_items",
            "invoices",
            "external_invoice_case_map",
            "clients",
            "business_profile",
        ):
            try:
                conn.execute(f"DELETE FROM {tbl}")
            except Exception:
                continue

        conn.execute(
            "INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute("INSERT INTO clients (id, name) VALUES (1, 'Client A')")

        conn.execute(
            """
            INSERT INTO client_deposit_ledger (
                business_profile_id, client_id, currency, amount_minor, entry_type, memo
            ) VALUES (1, 1, 'USD', 600, 'topup', 'seed-bp')
            """
        )
        conn.execute(
            """
            INSERT INTO client_deposit_ledger (
                business_profile_id, client_id, currency, amount_minor, entry_type, memo
            ) VALUES (NULL, 1, 'USD', 1000, 'topup', 'seed-global')
            """
        )

        conn.commit()
        conn.close()


def _extract_invoice_id_from_location(resp) -> int:
    loc = resp.headers.get("Location") or ""
    m = re.search("/invoices/(\\d+)", loc)
    assert m, f"unexpected redirect location: {loc}"
    return int(m.group(1))


def test_create_invoice_with_deposit_allows_draft_and_auto_publishes(admin_client, app):
    _seed_invoice_db_minimal(app)

    # Create invoice in draft but request deposit apply at create-time.
    resp = admin_client.post(
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
            "use_deposit": "1",
            # blank => apply as much as possible (up to outstanding)
            "deposit_amount": "",
            "deposit_memo": "",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 302
    invoice_id = _extract_invoice_id_from_location(resp)

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        inv = conn.execute(
            "SELECT billing_status, payment_status, payment_verified, status FROM invoices WHERE id=?",
            (invoice_id,),
        ).fetchone()
        assert inv is not None
        assert (inv["billing_status"] or "").strip().lower() == "sent"
        assert (inv["payment_status"] or "").strip().lower() == "paid"
        assert int(inv["payment_verified"] or 0) == 1
        # legacy status is derived; should be 'paid' when fully covered.
        assert (inv["status"] or "").strip().lower() == "paid"

        # Deposit ledger should include an apply entry linked to the invoice.
        s = conn.execute(
            """
            SELECT COALESCE(SUM(amount_minor), 0) AS s
            FROM client_deposit_ledger
            WHERE related_invoice_id=?
              AND entry_type IN ('apply','cancel_apply')
            """,
            (invoice_id,),
        ).fetchone()
        assert int(s["s"] or 0) < 0

        conn.close()


def test_create_regular_invoice_estimated_line_is_excluded_from_total(admin_client, app):
    _seed_invoice_db_minimal(app)

    resp = admin_client.post(
        "/accounting/invoice-system/invoices/new",
        data={
            "business_profile_id": "1",
            "client_id": "1",
            "issue_date": "2025-01-01",
            "due_date": "2025-02-01",
            "status": "draft",
            "invoice_language": "en",
            "description[]": ["Billable service", "Future estimate"],
            "qty[]": ["1", "1"],
            "unit_price[]": ["1000", "2000"],
            "item_type[]": ["service", "service"],
            "discount[]": ["0", "0"],
            "phase[]": ["app", "app"],
            "is_estimated_base[]": ["0", "1"],
        },
        follow_redirects=False,
    )

    assert resp.status_code == 302
    invoice_id = _extract_invoice_id_from_location(resp)

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        inv = conn.execute(
            "SELECT subtotal, tax, total FROM invoices WHERE id=?",
            (invoice_id,),
        ).fetchone()
        assert inv is not None
        assert float(inv["subtotal"]) == 1000.0
        assert float(inv["tax"]) == 100.0
        assert float(inv["total"]) == 1100.0

        rows = conn.execute(
            "SELECT description, is_estimated FROM line_items WHERE invoice_id=? ORDER BY id",
            (invoice_id,),
        ).fetchall()
        assert [(r["description"], int(r["is_estimated"] or 0)) for r in rows] == [
            ("Billable service", 0),
            ("Future estimate", 1),
        ]
        conn.close()


def test_create_invoice_deposit_blank_amount_applies_up_to_balance(admin_client, app):
    _seed_invoice_db_minimal(app)

    # Deposit balance is 110,000. Create a much larger invoice and apply deposit with blank amount.
    resp = admin_client.post(
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
            "unit_price[]": ["1000000"],
            "use_deposit": "1",
            # blank => apply up to deposit balance (partial)
            "deposit_amount": "",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 302
    invoice_id = _extract_invoice_id_from_location(resp)

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        inv = conn.execute(
            "SELECT billing_status, payment_status, payment_verified FROM invoices WHERE id=?",
            (invoice_id,),
        ).fetchone()
        assert inv is not None
        assert (inv["billing_status"] or "").strip().lower() == "sent"
        assert (inv["payment_status"] or "").strip().lower() == "pending"
        assert int(inv["payment_verified"] or 0) == 0

        s = conn.execute(
            """
            SELECT COALESCE(SUM(amount_minor), 0) AS s
            FROM client_deposit_ledger
            WHERE related_invoice_id=?
              AND entry_type IN ('apply','cancel_apply')
            """,
            (invoice_id,),
        ).fetchone()
        assert int(s["s"] or 0) == -110000
        conn.close()


def test_voiding_invoice_auto_cancels_applied_deposit_and_clears_payment(admin_client, app):
    _seed_invoice_db_minimal(app)

    # Create and auto-apply deposit.
    resp = admin_client.post(
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
            "use_deposit": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    invoice_id = _extract_invoice_id_from_location(resp)

    # Void via 2-axis bulk status change (billing).
    resp2 = admin_client.post(
        "/accounting/invoice-system/invoices/bulk_update_status",
        data={
            "mode": "billing",
            "new_status": "void",
            "invoice_ids[]": str(invoice_id),
        },
        follow_redirects=False,
    )
    assert resp2.status_code in (302, 303)

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()

        inv = conn.execute(
            "SELECT billing_status, payment_status, payment_verified, status FROM invoices WHERE id=?",
            (invoice_id,),
        ).fetchone()
        assert inv is not None
        assert (inv["billing_status"] or "").strip().lower() == "void"
        assert (inv["payment_status"] or "").strip().lower() == "none"
        assert int(inv["payment_verified"] or 0) == 0
        assert (inv["status"] or "").strip().lower() == "void"

        # Ensure all apply entries for this invoice are canceled.
        uncanceled = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM client_deposit_ledger a
            LEFT JOIN client_deposit_ledger c
              ON c.related_entry_id = a.id AND c.entry_type='cancel_apply'
            WHERE a.related_invoice_id=?
              AND a.entry_type='apply'
              AND c.id IS NULL
            """,
            (invoice_id,),
        ).fetchone()
        assert int(uncanceled["n"] or 0) == 0

        # Net effect of apply+cancel should be zero for this invoice.
        s = conn.execute(
            """
            SELECT COALESCE(SUM(amount_minor), 0) AS s
            FROM client_deposit_ledger
            WHERE related_invoice_id=?
              AND entry_type IN ('apply','cancel_apply')
            """,
            (invoice_id,),
        ).fetchone()
        assert int(s["s"] or 0) == 0

        conn.close()


def test_apply_deposit_on_draft_invoice_auto_publishes(admin_client, app):
    _seed_invoice_db_minimal(app)

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        # Create a draft invoice manually.
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date, due_date,
                status, billing_status, payment_status,
                subtotal_minor, tax_minor, total_minor,
                subtotal, tax, total, currency, vat_rate
            ) VALUES (
                1, 1, 1, 'INV-0001', '2025-01-01', '2025-02-01',
                'draft', 'draft', 'unpaid',
                1000, 100, 1100,
                1000, 100, 1100, 'USD', 10.0
            )
            """
        )
        conn.execute(
            "INSERT INTO line_items (invoice_id, description, qty, unit_price, item_type, discount, is_taxable) VALUES (1, 'Service', 1, 1000, 'service', 0, 1)"
        )
        conn.commit()
        conn.close()

    # Apply deposit from invoice view; should auto-publish to sent.
    resp = admin_client.post(
        "/accounting/invoice-system/invoices/1/deposit/apply",
        data={"amount": "", "memo": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with app.app_context():
        conn = get_db()
        inv = conn.execute(
            "SELECT billing_status, payment_status, payment_verified FROM invoices WHERE id=1"
        ).fetchone()
        assert (inv["billing_status"] or "").strip().lower() == "sent"
        assert (inv["payment_status"] or "").strip().lower() == "paid"
        assert int(inv["payment_verified"] or 0) == 1
        conn.close()


def test_apply_deposit_blank_amount_applies_up_to_balance(admin_client, app):
    _seed_invoice_db_minimal(app)

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        # Create a draft invoice that is larger than the deposit balance.
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date, due_date,
                status, billing_status, payment_status,
                subtotal_minor, tax_minor, total_minor,
                subtotal, tax, total, currency, vat_rate
            ) VALUES (
                1, 1, 1, 'INV-0002', '2025-01-01', '2025-02-01',
                'draft', 'draft', 'unpaid',
                1000000, 100000, 1100000,
                1000000, 100000, 1100000, 'USD', 10.0
            )
            """
        )
        conn.execute(
            "INSERT INTO line_items (invoice_id, description, qty, unit_price, item_type, discount, is_taxable) VALUES (1, 'Service', 1, 1000000, 'service', 0, 1)"
        )
        conn.commit()
        conn.close()

    # Apply deposit with blank amount; should apply up to balance and auto-publish to sent.
    resp = admin_client.post(
        "/accounting/invoice-system/invoices/1/deposit/apply",
        data={"amount": "", "memo": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with app.app_context():
        conn = get_db()
        inv = conn.execute(
            "SELECT billing_status, payment_status, payment_verified FROM invoices WHERE id=1"
        ).fetchone()
        assert (inv["billing_status"] or "").strip().lower() == "sent"
        assert (inv["payment_status"] or "").strip().lower() == "pending"
        assert int(inv["payment_verified"] or 0) == 0

        s = conn.execute(
            """
            SELECT COALESCE(SUM(amount_minor), 0) AS s
            FROM client_deposit_ledger
            WHERE related_invoice_id=?
              AND entry_type IN ('apply','cancel_apply')
            """,
            (1,),
        ).fetchone()
        assert int(s["s"] or 0) == -110000
        conn.close()


def test_apply_deposit_uses_global_deposit_pool_when_present(admin_client, app):
    _seed_invoice_db_minimal_global_deposit(app)

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        # Create a draft invoice manually (BP=1).
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date, due_date,
                status, billing_status, payment_status,
                subtotal_minor, tax_minor, total_minor,
                subtotal, tax, total, currency, vat_rate
            ) VALUES (
                1, 1, 1, 'INV-GLOBAL-0001', '2025-01-01', '2025-02-01',
                'draft', 'draft', 'unpaid',
                1000, 100, 1100,
                1000, 100, 1100, 'USD', 10.0
            )
            """
        )
        conn.execute(
            "INSERT INTO line_items (invoice_id, description, qty, unit_price, item_type, discount, is_taxable) VALUES (1, 'Service', 1, 1000, 'service', 0, 1)"
        )
        conn.commit()
        conn.close()

    resp = admin_client.post(
        "/accounting/invoice-system/invoices/1/deposit/apply",
        data={"amount": "", "memo": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with app.app_context():
        conn = get_db()
        inv = conn.execute(
            "SELECT billing_status, payment_status, payment_verified FROM invoices WHERE id=1"
        ).fetchone()
        assert (inv["billing_status"] or "").strip().lower() == "sent"
        assert (inv["payment_status"] or "").strip().lower() == "paid"
        assert int(inv["payment_verified"] or 0) == 1

        row = conn.execute(
            """
            SELECT business_profile_id, amount_minor
            FROM client_deposit_ledger
            WHERE related_invoice_id=? AND entry_type='apply'
            """,
            (1,),
        ).fetchone()
        assert row is not None
        assert row["business_profile_id"] is None
        assert int(row["amount_minor"] or 0) == -1100
        conn.close()


def test_apply_deposit_uses_bp_specific_then_global_pool(admin_client, app):
    _seed_invoice_db_minimal_mixed_deposit(app)

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date, due_date,
                status, billing_status, payment_status,
                subtotal_minor, tax_minor, total_minor,
                subtotal, tax, total, currency, vat_rate
            ) VALUES (
                1, 1, 1, 'INV-MIXED-0001', '2025-01-01', '2025-02-01',
                'draft', 'draft', 'unpaid',
                1000, 100, 1100,
                1000, 100, 1100, 'USD', 10.0
            )
            """
        )
        conn.execute(
            "INSERT INTO line_items (invoice_id, description, qty, unit_price, item_type, discount, is_taxable) VALUES (1, 'Service', 1, 1000, 'service', 0, 1)"
        )
        conn.commit()
        conn.close()

    resp = admin_client.post(
        "/accounting/invoice-system/invoices/1/deposit/apply",
        data={"amount": "", "memo": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with app.app_context():
        conn = get_db()
        rows = conn.execute(
            """
            SELECT business_profile_id, amount_minor
            FROM client_deposit_ledger
            WHERE related_invoice_id=? AND entry_type='apply'
            ORDER BY CASE WHEN business_profile_id IS NULL THEN 1 ELSE 0 END, id
            """,
            (1,),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["business_profile_id"] == 1
        assert int(rows[0]["amount_minor"] or 0) == -600
        assert rows[1]["business_profile_id"] is None
        assert int(rows[1]["amount_minor"] or 0) == -500
        conn.close()
