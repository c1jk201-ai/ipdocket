from pathlib import Path
from urllib.parse import parse_qs, urlparse


def _seed_invoice(
    app,
    *,
    billing_status: str = "draft",
    legacy_status: str = "draft",
    is_outgoing: int = 0,
    line_items: list[dict] | None = None,
) -> int:
    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()

        for tbl in (
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
                is_outgoing
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                1,
                1,
                "INV-001",
                "2026-01-01",
                "2026-01-31",
                legacy_status,
                billing_status,
                "unpaid",
                0,
                "USD",
                1000,
                1000,
                is_outgoing,
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
                    1,
                    item.get("description", "Service"),
                    float(item.get("qty", 1)),
                    float(item.get("unit_price", 0)),
                    item.get("item_type", "service"),
                    float(item.get("discount", 0)),
                    int(item.get("is_taxable", 1)),
                    item.get("phase", "app"),
                    int(item.get("is_estimated", 0)),
                ),
            )

        conn.commit()
        conn.close()
    return 1


def test_publish_and_print_redirect_preserves_fit_query(admin_client, app):
    invoice_id = _seed_invoice(app, billing_status="draft", legacy_status="draft")

    resp = admin_client.post(
        f"/accounting/invoice-system/invoices/{invoice_id}/publish_and_print",
        data={"lang": "en", "outgoing": "0", "fit": "1"},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 303)
    location = resp.headers.get("Location") or ""
    parsed = urlparse(location)
    q = parse_qs(parsed.query)

    assert parsed.path.endswith(f"/accounting/invoice-system/invoices/{invoice_id}")
    assert q.get("print") == ["1"]
    assert q.get("lang") == ["en"]
    assert q.get("outgoing") == ["0"]
    assert q.get("fit") == ["1"]


def test_publish_and_print_redirect_uses_stored_outgoing_mode_when_query_is_stale(
    admin_client, app
):
    invoice_id = _seed_invoice(
        app,
        billing_status="draft",
        legacy_status="draft",
        is_outgoing=1,
    )

    resp = admin_client.post(
        f"/accounting/invoice-system/invoices/{invoice_id}/publish_and_print",
        data={"lang": "en", "outgoing": "0", "fit": "1"},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 303)
    parsed = urlparse(resp.headers.get("Location") or "")
    q = parse_qs(parsed.query)

    assert parsed.path.endswith(f"/accounting/invoice-system/invoices/{invoice_id}")
    assert q.get("outgoing") == ["1"]


def test_publish_and_print_redirect_omits_fit_query_when_disabled(admin_client, app):
    invoice_id = _seed_invoice(app, billing_status="draft", legacy_status="draft")

    resp = admin_client.post(
        f"/accounting/invoice-system/invoices/{invoice_id}/publish_and_print",
        data={"lang": "en", "outgoing": "0", "fit": "0"},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 303)
    location = resp.headers.get("Location") or ""
    parsed = urlparse(location)
    q = parse_qs(parsed.query)

    assert parsed.path.endswith(f"/accounting/invoice-system/invoices/{invoice_id}")
    assert q.get("print") == ["1"]
    assert q.get("lang") == ["en"]
    assert q.get("outgoing") == ["0"]
    assert q.get("fit") is None


def test_view_invoice_renders_fit_hidden_field_for_publish(admin_client, app):
    invoice_id = _seed_invoice(app, billing_status="draft", legacy_status="draft")

    resp = admin_client.get(f"/accounting/invoice-system/invoices/{invoice_id}")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="publishAndPrintForm"' in html
    assert 'id="publishAndPrintFit"' in html
    assert 'name="fit"' in html


def test_view_invoice_renders_tax_complete_action_for_sent_invoice(admin_client, app):
    invoice_id = _seed_invoice(app, billing_status="sent", legacy_status="sent")

    resp = admin_client.get(f"/accounting/invoice-system/invoices/{invoice_id}")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "/accounting/invoice-system/taxinvoice/issue" not in html
    assert "/accounting/invoice-system/invoices/tax_issueNewq=INV-001" not in html
    assert f'action="/accounting/invoice-system/invoices/{invoice_id}/update_status"' in html
    assert 'name="status" value="tax_issued"' in html
    assert 'name="tax_issue_source" value="manual_detail"' in html
    assert "btn-tax-issue" in html
    assert "BP1" in html
    assert "Text" in html
    assert "Text" in html
    assert "Text" in html
    assert "Text" in html


def test_view_invoice_outgoing_shows_discount_in_domestic_phase_table(admin_client, app):
    invoice_id = _seed_invoice(
        app,
        billing_status="sent",
        legacy_status="sent",
        is_outgoing=1,
        line_items=[
            {
                "description": "Text Text",
                "qty": 4,
                "unit_price": 1000000,
                "item_type": "service",
                "discount": 15,
                "is_taxable": 1,
                "phase": "app",
                "is_estimated": 0,
            }
        ],
    )

    resp = admin_client.get(f"/accounting/invoice-system/invoices/{invoice_id}")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Text" in html
    assert "15.0%" in html


def test_view_invoice_outgoing_foreign_falls_back_when_fx_rate_missing(admin_client, app):
    invoice_id = _seed_invoice(
        app,
        billing_status="sent",
        legacy_status="sent",
        is_outgoing=1,
        line_items=[
            {
                "description": "FX fallback row",
                "qty": 1,
                "unit_price": 1_000_000,
                "item_type": "foreign",
                "discount": 20,
                "is_taxable": 0,
                "phase": "app",
                "is_estimated": 0,
            }
        ],
    )

    resp = admin_client.get(f"/accounting/invoice-system/invoices/{invoice_id}")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    idx = html.find("FX fallback row")
    assert idx >= 0
    snippet = html[idx : idx + 800]
    assert "800,000" in snippet


def test_invoice_print_css_wraps_description_column_for_pdf() -> None:
    source = Path("app/static/billing_invoices/invoice_print.css").read_text(encoding="utf-8")

    assert "body.invoice-print #invoice-print-container .invoice-print-table {" in source
    assert "table-layout: fixed !important;" in source
    assert (
        "body.invoice-print #invoice-print-container .invoice-print-table .cell-description,"
        in source
    )
    assert "white-space: normal !important;" in source
    assert "overflow-wrap: anywhere !important;" in source
    assert "body.invoice-print #invoice-print-container .invoice-print-table tfoot .num," in source
    assert "word-break: keep-all !important;" in source
    assert "overflow-wrap: normal !important;" in source


def test_invoice_view_marks_line_item_tables_for_print_wrapping(admin_client, app):
    invoice_id = _seed_invoice(
        app,
        billing_status="sent",
        legacy_status="sent",
        is_outgoing=1,
        line_items=[
            {
                "description": "Text Text Text_Text Text Text Text Text Text(CDS+ for manage)",
                "qty": 1,
                "unit_price": 400000,
                "item_type": "service",
                "discount": 0,
                "is_taxable": 1,
                "phase": "app",
                "is_estimated": 0,
            }
        ],
    )

    resp = admin_client.get(f"/accounting/invoice-system/invoices/{invoice_id}Newprint=1")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'class="invoice-print-table"' in html
    assert 'class="cell-description"' in html
