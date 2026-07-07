from __future__ import annotations

from flask import render_template


def _seed_invoice_view_data(
    app,
    *,
    invoices: list[dict],
    line_items: list[dict] | None = None,
    audit_logs: list[dict] | None = None,
    create_business_profile: bool = True,
):
    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()

        for tbl in (
            "audit_log",
            "invoice_attachments",
            "invoice_integrations",
            "invoice_revisions",
            "line_items",
            "external_invoice_case_map",
            "invoices",
            "clients",
            "business_profile",
        ):
            try:
                conn.execute(f"DELETE FROM {tbl}")
            except Exception:
                continue

        if create_business_profile:
            conn.execute(
                "INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 10.0, 1)"
            )

        conn.execute(
            "INSERT INTO clients (id, name, email, phone, address, manager) VALUES (1, 'Client A', 'client@example.com', '010-0000-0000', 'Seoul', 'Manager A')"
        )

        for invoice in invoices:
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
                    total,
                    total_minor,
                    vat_rate,
                    is_outgoing
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invoice.get("id", 1),
                    invoice.get("client_id", 1),
                    invoice.get("business_profile_id", 1),
                    invoice.get("number"),
                    invoice.get("issue_date", "2026-01-01"),
                    invoice.get("due_date", "2026-01-31"),
                    invoice.get("status", "draft"),
                    invoice.get("billing_status", "draft"),
                    invoice.get("payment_status", "unpaid"),
                    invoice.get("payment_verified", 0),
                    invoice.get("currency", "USD"),
                    invoice.get("total", 0),
                    invoice.get("total_minor", 0),
                    invoice.get("vat_rate", 10),
                    invoice.get("is_outgoing", 0),
                ),
            )

        for item in line_items or []:
            conn.execute(
                """
                INSERT INTO line_items (
                    invoice_id,
                    description,
                    qty,
                    unit_price,
                    item_type,
                    discount,
                    is_taxable,
                    phase,
                    is_estimated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.get("invoice_id", 1),
                    item.get("description", "Item"),
                    float(item.get("qty", 1)),
                    float(item.get("unit_price", 0)),
                    item.get("item_type", "service"),
                    float(item.get("discount", 0)),
                    int(item.get("is_taxable", 1)),
                    item.get("phase", "app"),
                    int(item.get("is_estimated", 0)),
                ),
            )

        for log in audit_logs or []:
            conn.execute(
                """
                INSERT INTO audit_log (action, target_type, target_id, meta, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    log.get("action"),
                    log.get("target_type"),
                    log.get("target_id"),
                    log.get("meta"),
                    log.get("created_at", "2026-01-01T00:00:00"),
                ),
            )

        conn.commit()
        conn.close()


def test_view_invoice_bulk_logs_do_not_match_partial_invoice_numbers(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_invoice_view_data(
        app,
        invoices=[
            {"id": 1, "number": "INV-001", "total": 1000, "total_minor": 1000},
            {"id": 2, "number": "INV-0011", "total": 1000, "total_minor": 1000},
        ],
        audit_logs=[
            {
                "action": "invoice.bulk_status_change",
                "meta": '{"count": 1, "invalid_count": 0, "mode": "payment", "new_status": "paid", "invoice_numbers": [\'INV-001\'], "invalid_numbers": []}',
                "created_at": "2026-01-03T09:00:00",
            },
            {
                "action": "invoice.bulk_status_change",
                "meta": '{"count": 1, "invalid_count": 0, "mode": "payment", "new_status": "pending", "invoice_numbers": [\'INV-0011\'], "invalid_numbers": []}',
                "created_at": "2026-01-04T09:00:00",
            },
        ],
    )

    resp = admin_client.get("/accounting/invoice-system/invoices/1/logsNewscope=payment")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Bulk change - Payment: paid" in html
    assert "Bulk change - Payment: pending" not in html


def test_view_invoice_usd_payment_panel_renders_without_client_context(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_invoice_view_data(
        app,
        invoices=[
            {
                "id": 1,
                "number": "FX-001",
                "currency": "USD",
                "total": 875,
                "total_minor": 87500,
            }
        ],
    )

    resp = admin_client.get("/accounting/invoice-system/invoices/1")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Payment verification" in html
    assert "<strong>Client:</strong> Client A" in html
    assert 'href="#sec-payment-usd"' in html
    assert 'id="sec-payment-usd"' in html
    assert 'aria-labelledby="paymentPanelTitleUsd"' in html


def test_view_invoice_ignores_stored_default_settlement_split(
    admin_client, app, clean_legacy_invoice_db
):
    from app.blueprints.billing_invoices.db import get_db

    _seed_invoice_view_data(
        app,
        invoices=[
            {
                "id": 1,
                "number": "SETTLE-DEFAULT",
                "currency": "USD",
                "total": 1000,
                "total_minor": 100000,
            }
        ],
    )

    with app.app_context():
        conn = get_db()
        conn.execute(
            "UPDATE invoices SET settlement_meta=? WHERE id=1",
            ('[{"business_profile_id": 1, "percent": 100}]',),
        )
        conn.commit()
        conn.close()

    resp = admin_client.get("/accounting/invoice-system/invoices/1")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Settlement business profile" not in html
    assert "Internal Settlement Done" not in html


def test_english_invoice_prefers_client_english_name_and_address(
    admin_client, app, clean_legacy_invoice_db
):
    from app.blueprints.billing_invoices.db import get_db

    _seed_invoice_view_data(
        app,
        invoices=[{"id": 1, "number": "INV-EN", "currency": "USD"}],
        line_items=[{"invoice_id": 1, "description": "Service fee", "unit_price": 1000}],
    )

    with app.app_context():
        conn = get_db()
        conn.execute(
            """
            UPDATE clients
               SET name=?,
                   address=?
             WHERE id=1
            """,
            (
                "Drauch Patent Attorney",
                "1200 New York Ave NW, Washington, DC, United States",
            ),
        )
        conn.commit()
        conn.close()

    resp = admin_client.get("/accounting/invoice-system/invoices/1Newlang=en")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Drauch Patent Attorney" in html
    assert "1200 New York Ave NW, Washington, DC, United States" in html


def test_localized_invoice_keeps_original_client_name_and_address(
    admin_client, app, clean_legacy_invoice_db
):
    from app.blueprints.billing_invoices.db import get_db

    _seed_invoice_view_data(
        app,
        invoices=[{"id": 1, "number": "INV-EN", "currency": "USD"}],
        line_items=[{"invoice_id": 1, "description": "Service fee", "unit_price": 1000}],
    )

    with app.app_context():
        conn = get_db()
        conn.execute(
            """
            UPDATE clients
               SET name=?,
                   address=?
             WHERE id=1
            """,
            (
                "Drauch Patent Attorney",
                "1200 New York Ave NW, Washington, DC, United States",
            ),
        )
        conn.commit()
        conn.close()

    resp = admin_client.get("/accounting/invoice-system/invoices/1")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Drauch Patent Attorney" in html
    assert "1200 New York Ave NW, Washington, DC, United States" in html


def test_publish_and_print_saves_selected_english_as_default_view_language(
    admin_client, app, clean_legacy_invoice_db
):
    from app.blueprints.billing_invoices.db import get_db, row_get

    _seed_invoice_view_data(
        app,
        invoices=[{"id": 1, "number": "INV-PUB-EN", "currency": "USD"}],
        line_items=[{"invoice_id": 1, "description": "Service fee", "unit_price": 1000}],
    )

    with app.app_context():
        conn = get_db()
        try:
            conn.execute("ALTER TABLE invoices ADD COLUMN language TEXT")
        except Exception:
            conn.rollback()
        conn.execute(
            """
            UPDATE clients
               SET name=?,
                   address=?
             WHERE id=1
            """,
            (
                "Drauch Patent Attorney",
                "1200 New York Ave NW, Washington, DC, United States",
            ),
        )
        conn.commit()
        conn.close()

    publish_resp = admin_client.post(
        "/accounting/invoice-system/invoices/1/publish_and_print",
        data={"lang": "en", "outgoing": "0"},
        follow_redirects=False,
    )
    assert publish_resp.status_code == 302
    assert "lang=en" in publish_resp.headers["Location"]

    with app.app_context():
        conn = get_db()
        row = conn.execute("SELECT language FROM invoices WHERE id=1").fetchone()
        assert row_get(row, "language", default=None) == "en"
        conn.close()

    view_resp = admin_client.get("/accounting/invoice-system/invoices/1")
    html = view_resp.get_data(as_text=True)

    assert view_resp.status_code == 200
    assert "Drauch Patent Attorney" in html
    assert "1200 New York Ave NW, Washington, DC, United States" in html


def test_view_invoice_usd_payment_anchor_uses_dedicated_id(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_invoice_view_data(
        app,
        invoices=[
            {
                "id": 1,
                "number": "USD-001",
                "currency": "USD",
                "total": 1000,
                "total_minor": 1000,
            }
        ],
    )

    resp = admin_client.get("/accounting/invoice-system/invoices/1")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'href="#sec-payment-usd"' in html
    assert 'id="sec-payment-usd"' in html
    assert 'aria-labelledby="paymentPanelTitleUsd"' in html


def test_view_invoice_outgoing_estimated_taxable_foreign_applies_vat(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_invoice_view_data(
        app,
        invoices=[
            {
                "id": 1,
                "number": "OUT-001",
                "is_outgoing": 1,
                "total": 0,
                "total_minor": 0,
            }
        ],
        line_items=[
            {
                "invoice_id": 1,
                "description": "Estimated foreign taxable",
                "qty": 1,
                "unit_price": 100000,
                "item_type": "foreign",
                "is_taxable": 1,
                "phase": "oa",
                "is_estimated": 1,
            }
        ],
    )

    resp = admin_client.get("/accounting/invoice-system/invoices/1")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Text" in html
    assert "110,000.00 USD" in html


def test_view_invoice_uses_stored_outgoing_mode_when_query_is_stale(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_invoice_view_data(
        app,
        invoices=[
            {
                "id": 1,
                "number": "OUT-STALE-QS",
                "is_outgoing": 1,
                "total": 1000,
                "total_minor": 1000,
            }
        ],
        line_items=[
            {
                "invoice_id": 1,
                "description": "Foreign service",
                "qty": 1,
                "unit_price": 1000,
                "item_type": "foreign",
                "is_taxable": 0,
                "phase": "app",
            }
        ],
    )

    resp = admin_client.get("/accounting/invoice-system/invoices/1Newlang=en&outgoing=0")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="invoice-print-container" class="invoice is-outgoing"' in html


def test_view_regular_invoice_renders_estimated_items_separately(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_invoice_view_data(
        app,
        invoices=[
            {
                "id": 1,
                "number": "REG-EST-001",
                "is_outgoing": 0,
                "total": 110,
                "total_minor": 110,
            }
        ],
        line_items=[
            {
                "invoice_id": 1,
                "description": "Regular billed service",
                "qty": 1,
                "unit_price": 100,
                "item_type": "service",
                "is_taxable": 1,
                "is_estimated": 0,
            },
            {
                "invoice_id": 1,
                "description": "Future service estimate",
                "qty": 1,
                "unit_price": 200,
                "item_type": "service",
                "is_taxable": 1,
                "is_estimated": 1,
            },
        ],
    )

    resp = admin_client.get("/accounting/invoice-system/invoices/1")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Regular billed service" in html
    assert "Future service estimate" in html
    assert "Text" in html
    assert "220.00 USD" in html
    assert "330.00 USD" not in html


def test_invoice_header_partial_tolerates_missing_business_profile(app):
    with app.test_request_context("/accounting/invoice-system/invoices/1"):
        html = render_template(
            "billing_invoices/partials/invoice_view/_header.html",
            biz_profile=None,
            invoice={
                "number": "NO-BP-001",
                "issue_date": "2026-01-01",
                "due_date": "2026-01-31",
                "billing_status": "draft",
                "client_name": "Client A",
                "client_phone": "",
                "client_manager": "",
                "ipm_case_id": None,
            },
            outgoing_mode=False,
            invoice_lang="en",
            items=[],
        )

    assert "Text(Text)" in html
