import json

from app.blueprints.billing_invoices.db import get_db, init_db


def _seed_invoice(
    conn,
    *,
    invoice_id: int,
    internal_reference: str = "",
    ipm_case_id: str = "",
    ipm_case_ref: str = "",
    is_outgoing: int = 0,
    status: str = "sent",
    payment_status: str = "unpaid",
    payment_verified: int = 0,
):
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
            subtotal,
            tax,
            total,
            subtotal_minor,
            tax_minor,
            total_minor,
            internal_reference,
            ipm_case_id,
            ipm_case_ref,
            is_outgoing
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            invoice_id,
            1,
            1,
            f"INV-{invoice_id:04d}",
            "2026-03-10",
            "2026-03-31",
            status,
            "sent",
            payment_status,
            payment_verified,
            "USD",
            1000,
            100,
            1100,
            1000,
            100,
            1100,
            internal_reference,
            ipm_case_id,
            ipm_case_ref,
            is_outgoing,
        ),
    )


def _extract_invoice_row(html: str, invoice_id: int) -> str:
    marker = f'id="inv-row-{invoice_id}"'
    assert marker in html
    return html.split(marker, 1)[1].split("</tr>", 1)[0]


def test_invoices_list_renders_linked_case_refs_instead_of_edit_input(
    admin_client, app, clean_legacy_invoice_db
):
    with app.app_context():
        init_db()
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (?, ?, ?, ?, ?)",
            (1, "BP1", "USD", 10.0, 1),
        )
        conn.execute("INSERT OR REPLACE INTO clients (id, name) VALUES (?, ?)", (1, "Client A"))
        _seed_invoice(conn, invoice_id=1, internal_reference="CLIENT-REF-001")
        conn.execute(
            "INSERT OR REPLACE INTO matter (matter_id, our_ref, right_name) VALUES (?, ?, ?)",
            ("MATTER-001", "26PD0103PCT", "Text Text Text"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO matter (matter_id, our_ref, right_name) VALUES (?, ?, ?)",
            ("MATTER-002", "26PD0104PCT", "Text Text Text"),
        )
        conn.execute(
            "INSERT INTO external_invoice_case_map (matter_id, our_ref, external_invoice_id) VALUES (?, ?, ?)",
            ("MATTER-001", "26PD0103PCT", 1),
        )
        conn.execute(
            "INSERT INTO external_invoice_case_map (matter_id, our_ref, external_invoice_id) VALUES (?, ?, ?)",
            ("MATTER-002", "26PD0104PCT", 1),
        )
        conn.commit()
        conn.close()

    resp = admin_client.get("/accounting/invoice-system/invoices")

    assert resp.status_code == 200
    row_html = _extract_invoice_row(resp.data.decode("utf-8", errors="replace"), 1)
    assert "/case/MATTER-001" in row_html
    assert "/case/MATTER-002" in row_html
    assert "invoice_id=1#sec-cost" in row_html
    assert "26PD0103PCT" in row_html
    assert "26PD0104PCT" in row_html
    assert "input-inline-ref" not in row_html


def test_invoices_list_orders_linked_cases_by_our_ref_desc(
    admin_client, app, clean_legacy_invoice_db
):
    with app.app_context():
        init_db()
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (?, ?, ?, ?, ?)",
            (1, "BP1", "USD", 10.0, 1),
        )
        conn.execute("INSERT OR REPLACE INTO clients (id, name) VALUES (?, ?)", (1, "Client A"))
        _seed_invoice(conn, invoice_id=10, internal_reference="CLIENT-REF-ORDER")
        for matter_id, our_ref, right_name in (
            ("LIST-252", "26TD0252US", "THERMAVAULT NP Text"),
            ("LIST-247", "26TD0247US", "CNTB"),
            ("LIST-250", "26TD0250US", "THERMAVAULT NP"),
            ("LIST-248", "26TD0248US", "CNTB Text"),
        ):
            conn.execute(
                "INSERT OR REPLACE INTO matter (matter_id, our_ref, right_name) VALUES (?, ?, ?)",
                (matter_id, our_ref, right_name),
            )
            conn.execute(
                "INSERT INTO external_invoice_case_map (matter_id, our_ref, external_invoice_id) VALUES (?, ?, ?)",
                (matter_id, our_ref, 10),
            )
        conn.commit()
        conn.close()

    resp = admin_client.get("/accounting/invoice-system/invoices")

    assert resp.status_code == 200
    row_html = _extract_invoice_row(resp.get_data(as_text=True), 10)
    assert row_html.index("26TD0252US") < row_html.index("26TD0250US")
    assert row_html.index("26TD0250US") < row_html.index("26TD0248US")
    assert row_html.index("26TD0248US") < row_html.index("26TD0247US")


def test_invoices_list_outgoing_filter_matches_invoice_flag_and_linked_outgoing_case(
    admin_client, app, clean_legacy_invoice_db
):
    with app.app_context():
        init_db()
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (?, ?, ?, ?, ?)",
            (1, "BP1", "USD", 10.0, 1),
        )
        conn.execute("INSERT OR REPLACE INTO clients (id, name) VALUES (?, ?)", (1, "Client A"))
        _seed_invoice(conn, invoice_id=1, is_outgoing=1)
        _seed_invoice(conn, invoice_id=2)
        _seed_invoice(conn, invoice_id=3)
        conn.execute(
            "INSERT OR REPLACE INTO matter (matter_id, our_ref, right_name, right_group, matter_type) VALUES (?, ?, ?, ?, ?)",
            ("MATTER-OUT", "26PO0001", "Text Text", "OUT", "PATENT"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO matter (matter_id, our_ref, right_name, right_group, matter_type) VALUES (?, ?, ?, ?, ?)",
            ("MATTER-DOM", "26PD0001", "Text Text", "DOM", "PATENT"),
        )
        conn.execute(
            "INSERT INTO external_invoice_case_map (matter_id, our_ref, external_invoice_id) VALUES (?, ?, ?)",
            ("MATTER-OUT", "26PO0001", 2),
        )
        conn.execute(
            "INSERT INTO external_invoice_case_map (matter_id, our_ref, external_invoice_id) VALUES (?, ?, ?)",
            ("MATTER-DOM", "26PD0001", 3),
        )
        conn.commit()
        conn.close()

    resp = admin_client.get("/accounting/invoice-system/invoicesNewoutgoing=1")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Text Text" in html
    assert "INV-0001" in html
    assert "INV-0002" in html
    assert "INV-0003" not in html

    export_resp = admin_client.get(
        "/accounting/invoice-system/invoices/exportNewoutgoing=1&format=json"
    )

    assert export_resp.status_code == 200
    exported = json.loads(export_resp.get_data(as_text=True))
    exported_numbers = {row["number"] for row in exported}
    assert exported_numbers == {"INV-0001", "INV-0002"}


def test_invoices_list_falls_back_to_primary_case_link_when_map_is_missing(
    admin_client, app, clean_legacy_invoice_db
):
    with app.app_context():
        init_db()
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (?, ?, ?, ?, ?)",
            (1, "BP1", "USD", 10.0, 1),
        )
        conn.execute("INSERT OR REPLACE INTO clients (id, name) VALUES (?, ?)", (1, "Client A"))
        conn.execute(
            "INSERT OR REPLACE INTO matter (matter_id, our_ref, right_name) VALUES (?, ?, ?)",
            ("MATTER-100", "26PD0105PCT", "Text Text Text"),
        )
        _seed_invoice(
            conn,
            invoice_id=1,
            internal_reference="CLIENT-REF-002",
            ipm_case_id="MATTER-100",
            ipm_case_ref="26PD0105PCT",
        )
        conn.commit()
        conn.close()

    resp = admin_client.get("/accounting/invoice-system/invoices")

    assert resp.status_code == 200
    row_html = _extract_invoice_row(resp.data.decode("utf-8", errors="replace"), 1)
    assert "/case/MATTER-100" in row_html
    assert "invoice_id=1#sec-cost" in row_html
    assert "26PD0105PCT" in row_html
    assert "input-inline-ref" not in row_html


def test_invoices_list_highlights_paid_invoice_without_case_link(
    admin_client, app, clean_legacy_invoice_db
):
    with app.app_context():
        init_db()
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (?, ?, ?, ?, ?)",
            (1, "BP1", "USD", 10.0, 1),
        )
        conn.execute("INSERT OR REPLACE INTO clients (id, name) VALUES (?, ?)", (1, "Client A"))
        _seed_invoice(conn, invoice_id=1, payment_status="paid", payment_verified=1)
        _seed_invoice(conn, invoice_id=2, payment_status="paid", payment_verified=1)
        conn.execute(
            "INSERT OR REPLACE INTO matter (matter_id, our_ref, right_name) VALUES (?, ?, ?)",
            ("MATTER-PAID", "26PD0201PCT", "Text Text Text"),
        )
        conn.execute(
            "INSERT INTO external_invoice_case_map (matter_id, our_ref, external_invoice_id) VALUES (?, ?, ?)",
            ("MATTER-PAID", "26PD0201PCT", 2),
        )
        conn.commit()
        conn.close()

    resp = admin_client.get("/accounting/invoice-system/invoices")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    row_without_case = _extract_invoice_row(html, 1)
    row_with_case = _extract_invoice_row(html, 2)
    assert "invoice-row-needs-case-link" in row_without_case
    assert 'data-needs-case-link="1"' in row_without_case
    assert "Matter reference required" in row_without_case
    assert "input-inline-case-missing" in row_without_case
    assert "invoice-row-needs-case-link" not in row_with_case
    assert 'data-needs-case-link="0"' in row_with_case


def test_invoices_list_case_filter_ignores_deleted_external_links(
    admin_client, app, clean_legacy_invoice_db
):
    with app.app_context():
        init_db()
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (?, ?, ?, ?, ?)",
            (1, "BP1", "USD", 10.0, 1),
        )
        conn.execute("INSERT OR REPLACE INTO clients (id, name) VALUES (?, ?)", (1, "Client A"))
        _seed_invoice(conn, invoice_id=1, internal_reference="CLIENT-REF-DELETED-LINK")
        conn.execute(
            "INSERT OR REPLACE INTO matter (matter_id, our_ref, right_name) VALUES (?, ?, ?)",
            ("MATTER-DELETED-LINK", "26PD9999PCT", "Text Text Text"),
        )
        conn.execute(
            """
            INSERT INTO external_invoice_case_map (
                matter_id, our_ref, external_invoice_id, is_deleted
            ) VALUES (?, ?, ?, ?)
            """,
            ("MATTER-DELETED-LINK", "26PD9999PCT", 1, True),
        )
        conn.commit()
        conn.close()

    resp = admin_client.get("/accounting/invoice-system/invoicesNewipm_case_id=MATTER-DELETED-LINK")

    assert resp.status_code == 200
    assert "INV-0001" not in resp.get_data(as_text=True)
