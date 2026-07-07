import json
import re


def _seed_minimal_invoice_data(app):
    """
    Insert a single invoice with:
    - service 1000 (taxable)
    - admin 200 (non-taxable conceptually, but VAT is computed at invoice level)
    - foreign 300 (non-taxable here)
    - VAT 10% -> tax 100 (service only)

    The business dashboard/service revenue logic should treat revenue as:
      service_subtotal = invoice.subtotal - admin - foreign = 1000
    (i.e., VAT excluded).
    """
    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()

        # Clean slate (raw SQL tables are outside SQLAlchemy metadata).
        conn.execute("DELETE FROM line_items")
        conn.execute("DELETE FROM invoices")
        conn.execute("DELETE FROM clients")
        conn.execute("DELETE FROM business_profile")

        conn.execute(
            "INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute(
            "INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (2, 'BP2', 'USD', 10.0, 1)"
        )
        conn.execute("INSERT INTO clients (id, name) VALUES (1, 'Client A')")

        # subtotal=1500, tax=100, total=1600
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date, due_date,
                status, billing_status, payment_status,
                subtotal, tax, total, currency, vat_rate, settlement_meta
            ) VALUES (
                1, 1, 1, 'INV-0001', '2024-06-01', '2024-06-30',
                'open', 'tax_issued', 'paid',
                1500, 100, 1600, 'USD', 10.0, ?
            )
            """,
            (
                json.dumps(
                    [
                        {"business_profile_id": 1, "percent": 50},
                        {"business_profile_id": 2, "percent": 50},
                    ]
                ),
            ),
        )

        # Line items are pre-tax; totals above must match these.
        conn.execute(
            "INSERT INTO line_items (invoice_id, description, qty, unit_price, item_type, discount, is_taxable, is_estimated) VALUES (1, 'Service', 1, 1000, 'service', 0, 1, 0)"
        )
        conn.execute(
            "INSERT INTO line_items (invoice_id, description, qty, unit_price, item_type, discount, is_taxable, is_estimated) VALUES (1, 'Admin', 1, 200, 'admin', 0, 0, 0)"
        )
        conn.execute(
            "INSERT INTO line_items (invoice_id, description, qty, unit_price, item_type, discount, is_taxable, is_estimated) VALUES (1, 'Foreign', 1, 300, 'foreign', 0, 0, 0)"
        )

        conn.commit()
        conn.close()


def test_api_summary_service_revenue_excludes_vat_issued_basis(admin_client, app):
    _seed_minimal_invoice_data(app)

    res = admin_client.get(
        "/accounting/invoice-system/api/summary?basis=issued&start_date=2024-01-01&end_date=2024-12-31"
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True

    rev = data["value"]["revenue"]
    assert rev["service_revenue_by_currency"]["USD"] == 1000.0
    assert rev["estimated_service_revenue_by_currency"]["USD"] == 1000.0
    assert rev["outstanding_service_revenue_by_currency"].get("USD", 0.0) == 0.0


def test_api_summary_service_revenue_excludes_vat_settlement_basis(admin_client, app):
    _seed_minimal_invoice_data(app)

    # Total (both BPs) should sum to the service subtotal (1000), not include VAT (1100).
    res_all = admin_client.get(
        "/accounting/invoice-system/api/summary?basis=settlement&start_date=2024-01-01&end_date=2024-12-31"
    )
    assert res_all.status_code == 200
    data_all = res_all.get_json()
    assert data_all["ok"] is True
    assert data_all["value"]["revenue"]["service_revenue_by_currency"]["USD"] == 1000.0

    # Selecting a single BP should return only its settlement share (50% => 500).
    res_bp1 = admin_client.get(
        "/accounting/invoice-system/api/summary?basis=settlement&business_profile_id=1&start_date=2024-01-01&end_date=2024-12-31"
    )
    assert res_bp1.status_code == 200
    data_bp1 = res_bp1.get_json()
    assert data_bp1["ok"] is True
    assert data_bp1["value"]["revenue"]["service_revenue_by_currency"]["USD"] == 500.0


def test_business_dashboard_renders_cumulative_and_bar_revenue_charts(admin_client, app):
    _seed_minimal_invoice_data(app)

    res = admin_client.get("/business/?basis=settlement&start_date=2024-01-01&end_date=2024-12-31")
    assert res.status_code == 200

    html = res.get_data(as_text=True)
    assert 'id="revenueTrendChart"' in html
    assert 'id="revenueTrendBarChart"' in html
    assert "const cumulativeSeriesEstimated =" in html
    assert "const periodSeriesEstimated =" in html


def test_business_dashboard_settlement_recent_rows_prorate_vat_and_costs(admin_client, app):
    _seed_minimal_invoice_data(app)

    res = admin_client.get(
        "/business/?basis=settlement&business_profile_id=1&start_date=2024-01-01&end_date=2024-12-31"
    )
    assert res.status_code == 200

    html = res.get_data(as_text=True)
    assert "800.00 USD" in html
    assert re.search(
        r"\(Service\s+500,\s+Sales tax\s+50,\s+Official fee\s+100,\s+Foreign\s+150\)",
        html,
    )


def test_invoices_list_settlement_basis_prorates_display_amounts(admin_client, app):
    _seed_minimal_invoice_data(app)

    res = admin_client.get(
        "/accounting/invoice-system/invoices"
        "?basis=settlement&business_profile_id=1&date_from=2024-01-01&date_to=2024-12-31"
    )
    assert res.status_code == 200

    html = res.get_data(as_text=True)
    assert "INV-0001" in html
    assert "800.00 USD" in html
    assert "1,600.00 USD" not in html
    assert re.search(r"\(Svc\+tax\s+550,\s+fees\s+100,\s+foreign\s+150\)", html)


def test_invoice_export_settlement_basis_prorates_json_amounts(admin_client, app):
    _seed_minimal_invoice_data(app)

    res = admin_client.get(
        "/accounting/invoice-system/invoices/export"
        "?format=json&basis=settlement&business_profile_id=1"
        "&date_from=2024-01-01&date_to=2024-12-31"
    )
    assert res.status_code == 200

    data = json.loads(res.get_data(as_text=True))
    assert len(data) == 1
    assert data[0]["number"] == "INV-0001"
    assert data[0]["subtotal"] == 750.0
    assert data[0]["tax"] == 50.0
    assert data[0]["total"] == 800.0


def test_default_settlement_split_is_not_rendered_as_explicit_summary(
    admin_client, app
):
    from app.blueprints.billing_invoices.db import get_db

    _seed_minimal_invoice_data(app)
    with app.app_context():
        conn = get_db()
        conn.execute(
            "UPDATE invoices SET settlement_meta=? WHERE id=1",
            (json.dumps([{"business_profile_id": 1, "percent": 100}]),),
        )
        conn.commit()
        conn.close()

    list_res = admin_client.get(
        "/accounting/invoice-system/invoices?date_from=2024-01-01&date_to=2024-12-31"
    )
    assert list_res.status_code == 200
    html = list_res.get_data(as_text=True)
    assert "BP1 100%" not in html

    export_res = admin_client.get(
        "/accounting/invoice-system/invoices/export"
        "?format=json&date_from=2024-01-01&date_to=2024-12-31"
    )
    assert export_res.status_code == 200
    data = json.loads(export_res.get_data(as_text=True))
    assert data[0]["settlement_summary"] == "BP1"
