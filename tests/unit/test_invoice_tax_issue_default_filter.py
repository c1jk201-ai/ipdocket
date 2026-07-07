from bs4 import BeautifulSoup

from app.blueprints.billing_invoices.db import get_db, init_db


def _seed_tax_issue_invoices(app) -> None:
    with app.app_context():
        init_db()
        conn = get_db()
        for table in (
            "line_items",
            "invoices",
            "external_invoice_case_map",
            "matter",
            "clients",
            "business_profile",
        ):
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                continue

        conn.execute(
            """
            INSERT OR REPLACE INTO business_profile
                (id, name, currency, vat_rate, next_invoice_no)
            VALUES
                (1, 'USD BP', 'USD', 10.0, 1),
                (2, 'USD BP', 'USD', 0.0, 1)
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO clients (
                id, name, biz_tax_invoice_email, email,
                biz_reg_number, biz_company_name, biz_representative_name
            )
            VALUES
                (1, 'USD Client', 'usd-tax@example.com', 'usd@example.com', '1112233333', 'USD Client Co', 'USD CEO'),
                (2, 'USD Client', 'usd-tax@example.com', 'usd@example.com', '2223344444', 'USD Client Co', 'USD CEO')
            """
        )
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
                vat_rate,
                subtotal,
                tax,
                total,
                subtotal_minor,
                tax_minor,
                total_minor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                1,
                1,
                "INV-USD-ZERO",
                "2026-04-01",
                "2026-04-30",
                "sent",
                "sent",
                "paid",
                1,
                "USD",
                10.0,
                100000,
                10000,
                110000,
                100000,
                10000,
                110000,
            ),
        )
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
                vat_rate,
                subtotal,
                tax,
                total,
                subtotal_minor,
                tax_minor,
                total_minor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                2,
                2,
                2,
                "INV-USD-001",
                "2026-04-02",
                "2026-05-02",
                "sent",
                "sent",
                "paid",
                1,
                "USD",
                0.0,
                1000,
                0,
                1000,
                100000,
                0,
                100000,
            ),
        )
        conn.execute(
            """
            INSERT INTO line_items (
                invoice_id, description, qty, unit_price, item_type, discount, is_taxable, is_estimated
            ) VALUES
                (1, 'USD Service', 1, 100000, 'service', 0, 1, 0),
                (2, 'USD Service', 1, 1000, 'service', 0, 1, 0)
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO matter (matter_id, our_ref, right_name)
            VALUES
                ('M1', 'CASE-1', 'USD Tax Issue Matter'),
                ('M2', 'CASE-2', 'USD Tax Issue Matter')
            """
        )
        conn.execute(
            """
            INSERT INTO external_invoice_case_map (matter_id, our_ref, external_invoice_id)
            VALUES
                ('M1', 'CASE-1', 1),
                ('M2', 'CASE-2', 2)
            """
        )
        conn.commit()
        conn.close()


def test_tax_issue_defaults_to_usd_businesses(admin_client, app, clean_legacy_invoice_db):
    _seed_tax_issue_invoices(app)

    response = admin_client.get("/accounting/invoice-system/invoices/tax_issue")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")
    selector = soup.find("select", attrs={"name": "business_profile_id"})
    assert selector is not None
    selected = selector.find("option", selected=True)
    assert selected is not None
    assert selected.get("value") == "C:USD"
    batch_form = soup.find("form", attrs={"id": "taxIssueBatchForm"})
    assert batch_form is not None
    assert batch_form.get("action") == "/accounting/invoice-system/invoices/tax_issue/confirm"
    assert "legacy provider" not in html
    assert "Tax documentation queue" in html
    assert batch_form.find("input", attrs={"name": "mode", "value": "billing"}) is None
    assert batch_form.find("input", attrs={"name": "new_status", "value": "tax_issued"}) is None
    assert "INV-USD-001" in html
    assert "INV-USD-ZERO" in html


def test_tax_issue_page_shows_tax_invoice_address_without_invoice_info_badge(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_tax_issue_invoices(app)

    with app.app_context():
        conn = get_db()
        conn.execute(
            """
            UPDATE clients
               SET extra=?,
                   biz_business_location=?
             WHERE id=1
            """,
            (
                '{"tax_address": "Text Text Text 1"}',
                "Text Text Text 2",
            ),
        )
        conn.commit()
        conn.close()

    response = admin_client.get("/accounting/invoice-system/invoices/tax_issue")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")
    assert "Text Text" in html
    assert "Tax profile address" in html
    assert "Text Text Text 1" in html
    assert "Text Text Text 2" not in html
    assert (
        soup.find("button", attrs={"data-copy-value": "Text Text Text 1"}) is not None
    )


def test_tax_issue_page_uses_business_number_not_corporate_registration_number(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_tax_issue_invoices(app)

    with app.app_context():
        conn = get_db()
        conn.execute(
            """
            UPDATE clients
               SET biz_reg_number='',
                   registration_number=?,
                   extra=?
             WHERE id=1
            """,
            (
                "110111-1234567",
                '{"business_reg_no": "123-45-67890"}',
            ),
        )
        conn.commit()
        conn.close()

    response = admin_client.get("/accounting/invoice-system/invoices/tax_issue")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")
    assert "Text" in html
    assert "123-45-67890" in html
    assert "110111-1234567" not in html
    assert soup.find("button", attrs={"data-copy-value": "123-45-67890"}) is not None


def test_tax_issue_page_shows_personal_registration_number_for_individual_client(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_tax_issue_invoices(app)

    with app.app_context():
        conn = get_db()
        conn.execute(
            """
            UPDATE clients
               SET type='individual',
                   biz_reg_number='',
                   registration_number=?,
                   extra=NULL
             WHERE id=1
            """,
            ("900101-1234567",),
        )
        conn.commit()
        conn.close()

    response = admin_client.get("/accounting/invoice-system/invoices/tax_issue")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")
    assert "Personal / registration number" in html
    assert "900101-1234567" in html
    assert soup.find("button", attrs={"data-copy-value": "900101-1234567"}) is not None


def test_tax_issue_page_confirm_marks_status_after_hometax(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_tax_issue_invoices(app)

    response = admin_client.post(
        "/accounting/invoice-system/invoices/tax_issue/confirm",
        data={
            "invoice_ids[]": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    with app.app_context():
        conn = get_db()
        row = conn.execute(
            """
            SELECT status, billing_status, tax_issued_at, tax_issue_type, tax_issue_source
            FROM invoices
            WHERE id=1
            """
        ).fetchone()
        conn.close()

    assert row is not None
    assert (row["status"] or "").strip().lower() == "tax_issued"
    assert (row["billing_status"] or "").strip().lower() == "tax_issued"
    assert row["tax_issued_at"]
    assert row["tax_issue_type"] == "tax_invoice"
    assert row["tax_issue_source"] == "tax_issue_page"


def test_tax_issue_respects_explicit_currency_group_filter(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_tax_issue_invoices(app)

    response = admin_client.get(
        "/accounting/invoice-system/invoices/tax_issueNewbusiness_profile_id=C:USD"
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")
    selector = soup.find("select", attrs={"name": "business_profile_id"})
    assert selector is not None
    selected = selector.find("option", selected=True)
    assert selected is not None
    assert selected.get("value") == "C:USD"
    assert "INV-USD-001" in html
    assert "INV-USD-ZERO" in html


def test_tax_issue_page_disables_rows_that_batch_issue_would_reject(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_tax_issue_invoices(app)

    with app.app_context():
        conn = get_db()
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
                vat_rate,
                subtotal,
                tax,
                total,
                subtotal_minor,
                tax_minor,
                total_minor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                3,
                1,
                1,
                "INV-USD-NOCASE",
                "2026-04-03",
                "2026-05-03",
                "sent",
                "sent",
                "paid",
                1,
                "USD",
                10.0,
                100000,
                10000,
                110000,
                100000,
                10000,
                110000,
            ),
        )
        conn.execute(
            """
            INSERT INTO line_items (
                invoice_id, description, qty, unit_price, item_type, discount, is_taxable, is_estimated
            ) VALUES (3, 'Ready except case', 1, 100000, 'service', 0, 1, 0)
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO matter (matter_id, our_ref, right_name)
            VALUES ('M-READY-1', 'READY-1', 'Ready Tax Issue Matter')
            """
        )
        conn.execute(
            """
            INSERT INTO external_invoice_case_map (matter_id, our_ref, external_invoice_id)
            VALUES ('M-READY-1', 'READY-1', 1)
            """
        )
        conn.commit()
        conn.close()

    response = admin_client.get("/accounting/invoice-system/invoices/tax_issue")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")
    ready_input = soup.find("input", attrs={"name": "invoice_ids[]", "value": "1"})
    blocked_input = soup.find("input", attrs={"name": "invoice_ids[]", "value": "3"})
    assert ready_input is not None
    assert blocked_input is not None
    assert not ready_input.has_attr("disabled")
    assert blocked_input.has_attr("disabled")
    assert soup.find("span", class_="meta-pill is-warning") is not None
    assert "Review required:" in html
