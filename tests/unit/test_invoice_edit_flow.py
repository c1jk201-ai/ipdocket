def _seed_editable_invoice(
    app,
    *,
    status: str = "sent",
    billing_status: str = "sent",
    payment_status: str = "unpaid",
    payment_verified: int = 0,
    with_deposit: bool = False,
) -> None:
    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()
        conn.execute(
            "INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute("INSERT INTO clients (id, name) VALUES (1, 'Client A')")
        if with_deposit:
            conn.execute(
                """
                INSERT INTO client_deposit_ledger (
                    business_profile_id, client_id, currency, amount_minor, entry_type, memo
                ) VALUES (1, 1, 'USD', 330000, 'topup', 'seed')
                """
            )
        conn.execute(
            """
            INSERT INTO invoices (
                id,
                client_id,
                business_profile_id,
                number,
                internal_reference,
                issue_date,
                due_date,
                status,
                billing_status,
                payment_status,
                payment_verified,
                notes,
                currency,
                vat_rate,
                subtotal,
                tax,
                total,
                subtotal_minor,
                tax_minor,
                total_minor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                1,
                1,
                "INV-EDIT-0001",
                "OLD-REF",
                "2026-02-21",
                "2026-03-21",
                status,
                billing_status,
                payment_status,
                payment_verified,
                "old note",
                "USD",
                10.0,
                1000,
                100,
                1100,
                1000,
                100,
                1100,
            ),
        )
        conn.execute(
            """
            INSERT INTO line_items (
                invoice_id, description, qty, unit_price, item_type, discount, is_taxable
            ) VALUES (1, 'Original service', 1, 1000, 'service', 0, 1)
            """
        )
        conn.commit()
        conn.close()


def test_edit_invoice_updates_totals_replaces_items_and_applies_deposit(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_editable_invoice(app, with_deposit=True)

    resp = admin_client.post(
        "/accounting/invoice-system/invoices/1/edit",
        data={
            "business_profile_id": "1",
            "client_id": "1",
            "number": "INV-EDIT-0001",
            "internal_reference": "REF-NEW",
            "issue_date": "2026-02-22",
            "due_date": "2026-03-22",
            "status": "sent",
            "notes": "updated note",
            "invoice_language": "en",
            "description[]": ["Updated service"],
            "qty[]": ["2"],
            "unit_price[]": ["1500"],
            "use_deposit": "1",
            "deposit_amount": "",
            "deposit_memo": "edit apply",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].split("?", 1)[0].endswith(
        "/accounting/invoice-system/invoices/1"
    )

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        inv = conn.execute(
            """
            SELECT
                number,
                internal_reference,
                notes,
                status,
                billing_status,
                payment_status,
                payment_verified,
                subtotal_minor,
                tax_minor,
                total_minor
            FROM invoices
            WHERE id=1
            """
        ).fetchone()
        assert inv is not None
        assert inv["number"] == "INV-EDIT-0001"
        assert inv["internal_reference"] == "REF-NEW"
        assert inv["notes"] == "updated note"
        assert (inv["status"] or "").strip().lower() == "paid"
        assert (inv["billing_status"] or "").strip().lower() == "sent"
        assert (inv["payment_status"] or "").strip().lower() == "paid"
        assert int(inv["payment_verified"] or 0) == 1
        assert int(inv["subtotal_minor"] or 0) == 300000
        assert int(inv["tax_minor"] or 0) == 30000
        assert int(inv["total_minor"] or 0) == 330000

        items = conn.execute(
            "SELECT description, qty, unit_price FROM line_items WHERE invoice_id=1 ORDER BY id"
        ).fetchall()
        assert len(items) == 1
        assert items[0]["description"] == "Updated service"
        assert float(items[0]["qty"] or 0) == 2.0
        assert float(items[0]["unit_price"] or 0) == 1500.0

        ledger_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount_minor), 0) AS s
            FROM client_deposit_ledger
            WHERE related_invoice_id=1
              AND entry_type IN ('apply', 'cancel_apply')
            """
        ).fetchone()
        assert int(ledger_sum["s"] or 0) == -330000
        conn.close()


def test_edit_invoice_ignores_stored_default_settlement_split(
    admin_client, app, clean_legacy_invoice_db
):
    from app.blueprints.billing_invoices.db import get_db

    _seed_editable_invoice(app)
    with app.app_context():
        conn = get_db()
        conn.execute(
            "UPDATE invoices SET settlement_meta=? WHERE id=1",
            ('[{"business_profile_id": 1, "percent": 100}]',),
        )
        conn.commit()
        conn.close()

    resp = admin_client.get("/accounting/invoice-system/invoices/1/edit")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "window.initialSettlementSplits = null;" in html


def _replace_invoice_with_distinct_outgoing_items(app) -> None:
    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        conn.execute("DELETE FROM line_items WHERE invoice_id=1")
        conn.execute(
            """
            UPDATE invoices
               SET subtotal=5354952,
                   tax=195000,
                   total=5549952,
                   subtotal_minor=535495200,
                   tax_minor=19500000,
                   total_minor=554995200,
                   is_outgoing=1,
                   currency='USD',
                   vat_rate=10
             WHERE id=1
            """
        )
        conn.execute(
            """
            INSERT INTO client_deposit_ledger (
                business_profile_id, client_id, currency, amount_minor, entry_type, memo
            ) VALUES (1, 1, 'USD', 600000000, 'topup', 'extra seed')
            """
        )
        rows = [
            (
                "Text",
                1,
                3404952,
                "foreign",
                0,
                0,
                "app",
                "USD",
                1550,
                730,
                0,
                1493.4,
            ),
            (
                "Text",
                1,
                800000,
                "service",
                0,
                1,
                "app",
                None,
                0,
                0,
                None,
                None,
            ),
            (
                "Text Text Text",
                1,
                1150000,
                "service",
                0,
                1,
                "app",
                None,
                0,
                0,
                None,
                None,
            ),
        ]
        for row in rows:
            conn.execute(
                """
                INSERT INTO line_items (
                    invoice_id, description, qty, unit_price, item_type, discount,
                    is_taxable, phase, fx_currency, fx_fee, fx_gov, fx_markup, fx_rate_used,
                    is_estimated
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                row,
            )
        conn.commit()
        conn.close()


def test_edit_invoice_uses_stored_outgoing_mode_when_query_is_stale(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_editable_invoice(app, with_deposit=True)
    _replace_invoice_with_distinct_outgoing_items(app)

    resp = admin_client.get("/accounting/invoice-system/invoices/1/editNewoutgoing=0")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'name="is_outgoing" id="is_outgoing" value="1"' in html


def test_edit_invoice_with_deposit_allows_amount_changes_when_line_structure_is_preserved(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_editable_invoice(app, with_deposit=True)
    _replace_invoice_with_distinct_outgoing_items(app)

    resp = admin_client.post(
        "/accounting/invoice-system/invoices/1/edit?outgoing=1",
        data={
            "business_profile_id": "1",
            "client_id": "1",
            "number": "INV-EDIT-0001",
            "internal_reference": "REF-NEW",
            "issue_date": "2026-02-22",
            "due_date": "2026-03-22",
            "status": "sent",
            "notes": "updated note",
            "invoice_language": "en",
            "is_outgoing": "1",
            "description[]": [
                "Text",
                "Text",
                "Text Text Text",
            ],
            "qty[]": ["1", "1", "1"],
            "unit_price[]": ["3404952", "800000", "1150000"],
            "item_type[]": ["foreign", "service", "service"],
            "discount[]": ["0", "0", "0"],
            "phase[]": ["app", "app", "app"],
            "fx_currency[]": ["USD", "", ""],
            "fx_fee[]": ["1550", "0", "0"],
            "fx_gov[]": ["730", "0", "0"],
            "fx_markup[]": ["0", "", ""],
            "fx_rate_used[]": ["1493.4", "", ""],
            "is_estimated_base[]": ["0", "0", "0"],
            "foreign_vat_base[]": ["0", "0", "0"],
            "use_deposit": "1",
            "deposit_amount": "",
            "deposit_memo": "edit apply",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].split("?", 1)[0].endswith(
        "/accounting/invoice-system/invoices/1"
    )

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        inv = conn.execute(
            "SELECT total_minor, payment_status, payment_verified FROM invoices WHERE id=1"
        ).fetchone()
        assert int(inv["total_minor"] or 0) == 554995200
        assert (inv["payment_status"] or "").strip().lower() == "paid"
        assert int(inv["payment_verified"] or 0) == 1
        rows = conn.execute(
            "SELECT description, item_type FROM line_items WHERE invoice_id=1 ORDER BY id"
        ).fetchall()
        assert [(r["description"], r["item_type"]) for r in rows] == [
            ("Text", "foreign"),
            ("Text", "service"),
            ("Text Text Text", "service"),
        ]
        conn.close()


def test_edit_invoice_with_deposit_blocks_collapsed_line_structure(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_editable_invoice(app, with_deposit=True)
    _replace_invoice_with_distinct_outgoing_items(app)

    resp = admin_client.post(
        "/accounting/invoice-system/invoices/1/edit?outgoing=1",
        data={
            "business_profile_id": "1",
            "client_id": "1",
            "number": "INV-EDIT-0001",
            "internal_reference": "REF-NEW",
            "issue_date": "2026-02-22",
            "due_date": "2026-03-22",
            "status": "sent",
            "notes": "updated note",
            "invoice_language": "en",
            "is_outgoing": "1",
            "description[]": [
                "Text Text Text",
                "Text Text Text",
                "Text Text Text",
            ],
            "qty[]": ["1", "1", "1"],
            "unit_price[]": ["1150000", "1150000", "1150000"],
            "item_type[]": ["service", "service", "service"],
            "discount[]": ["0", "0", "0"],
            "phase[]": ["app", "app", "app"],
            "is_estimated_base[]": ["0", "0", "0"],
            "foreign_vat_base[]": ["0", "0", "0"],
            "use_deposit": "1",
            "deposit_amount": "",
            "deposit_memo": "edit apply",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(
        "/accounting/invoice-system/invoices/1/edit?outgoing=1"
    )

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        inv = conn.execute(
            "SELECT total_minor, payment_status, payment_verified FROM invoices WHERE id=1"
        ).fetchone()
        assert int(inv["total_minor"] or 0) == 554995200
        assert (inv["payment_status"] or "").strip().lower() == "unpaid"
        assert int(inv["payment_verified"] or 0) == 0
        rows = conn.execute(
            "SELECT description, item_type FROM line_items WHERE invoice_id=1 ORDER BY id"
        ).fetchall()
        assert [(r["description"], r["item_type"]) for r in rows] == [
            ("Text", "foreign"),
            ("Text", "service"),
            ("Text Text Text", "service"),
        ]
        apply_count = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM client_deposit_ledger
            WHERE related_invoice_id=1 AND entry_type='apply'
            """
        ).fetchone()
        assert int(apply_count["n"] or 0) == 0
        conn.close()


def test_edit_invoice_redirects_when_payment_is_verified(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_editable_invoice(
        app,
        status="paid",
        billing_status="sent",
        payment_status="paid",
        payment_verified=1,
    )

    resp = admin_client.get(
        "/accounting/invoice-system/invoices/1/edit",
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/accounting/invoice-system/invoices/1")


def test_edit_invoice_creates_new_client_with_name_en_extra(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_editable_invoice(app)

    response = admin_client.post(
        "/accounting/invoice-system/invoices/1/edit",
        data={
            "business_profile_id": "1",
            "client_id": "",
            "new_client_name": "Drauch Patent Attorney",
            "new_client_name_en": "Drauch Patent Attorney",
            "new_client_email": "stefanie.assmann@rauch-ip.de",
            "new_client_phone": "+4960324031",
            "new_client_address": "Frankfurter Str. 34, 61231 Bad Nauheim, Germany",
            "new_client_manager": "Stefanie Assmann",
            "new_client_notes": "foreign client",
            "number": "INV-EDIT-0001",
            "internal_reference": "REF-NEW",
            "issue_date": "2026-02-22",
            "due_date": "2026-03-22",
            "status": "sent",
            "notes": "updated note",
            "invoice_language": "en",
            "description[]": ["Updated service"],
            "qty[]": ["1"],
            "unit_price[]": ["1500"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 302

    from app.blueprints.billing_invoices.db import get_db, safe_json_parse

    with app.app_context():
        conn = get_db()
        client_row = conn.execute(
            "SELECT id, name, email, phone, address, manager, notes, extra FROM clients WHERE name=?",
            ("Drauch Patent Attorney",),
        ).fetchone()
        assert client_row is not None
        extra = safe_json_parse(client_row["extra"], {}) or {}
        assert client_row["email"] == "stefanie.assmann@rauch-ip.de"
        assert client_row["phone"] == "+4960324031"
        assert client_row["address"] == "Frankfurter Str. 34, 61231 Bad Nauheim, Germany"
        assert client_row["manager"] == "Stefanie Assmann"
        assert client_row["notes"] == "foreign client"
        assert extra.get("name_en") == "Drauch Patent Attorney"

        invoice_row = conn.execute("SELECT client_id FROM invoices WHERE id=1").fetchone()
        assert int(invoice_row["client_id"]) == int(client_row["id"])
        conn.close()
