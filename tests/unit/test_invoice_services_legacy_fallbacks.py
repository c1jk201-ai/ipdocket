import json

from app.blueprints.billing_invoices.db import get_db, init_db
from app.services.billing.invoice_services import InvoiceLinkService, InvoiceService, PaymentService


def _seed_minimal_invoice(
    conn,
    *,
    invoice_id: int = 1,
    status: str = "sent",
    billing_status: str = "sent",
    payment_status: str = "unpaid",
    payment_verified: int = 0,
    total_minor: int = 1100,
    payment_meta: str | None = None,
):
    conn.execute(
        "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (?, ?, ?, ?, ?)",
        (1, "BP1", "USD", 10.0, 1),
    )
    conn.execute("INSERT OR REPLACE INTO clients (id, name) VALUES (?, ?)", (1, "Client A"))
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
            payment_meta
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            invoice_id,
            1,
            1,
            f"INV-{invoice_id:04d}",
            "2026-03-10",
            "2026-03-31",
            status,
            billing_status,
            payment_status,
            payment_verified,
            "USD",
            1000,
            100,
            total_minor,
            1000,
            100,
            total_minor,
            payment_meta,
        ),
    )


def test_payment_totals_fall_back_to_paid_invoice_state(app, clean_legacy_invoice_db):
    with app.app_context():
        init_db()
        conn = get_db()
        _seed_minimal_invoice(
            conn,
            status="paid",
            payment_status="paid",
            payment_verified=1,
            total_minor=1100,
        )
        conn.commit()
        conn.close()

        assert PaymentService.get_total_paid(1) == 1100
        totals = InvoiceService.calculate_totals(1)
        assert totals["paid_total"] == 1100
        assert totals["outstanding"] == 0

        payments = PaymentService.get_payments(1)
        assert len(payments) == 1
        assert payments[0].amount_minor == 1100
        assert payments[0].method == "status"
        assert payments[0].verified is True


def test_payment_totals_use_payment_meta_and_deposit_ledger(app, clean_legacy_invoice_db):
    with app.app_context():
        init_db()
        conn = get_db()
        _seed_minimal_invoice(
            conn,
            invoice_id=1,
            total_minor=1100,
            payment_meta=json.dumps({"deposit": "400", "currency": "USD"}),
        )
        _seed_minimal_invoice(conn, invoice_id=2, total_minor=1100)
        conn.execute(
            """
            INSERT INTO client_deposit_ledger (
                business_profile_id,
                client_id,
                currency,
                amount_minor,
                entry_type,
                memo,
                related_invoice_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (1, 1, "USD", -500, "apply", "legacy apply", 2),
        )
        conn.commit()
        conn.close()

        meta_totals = InvoiceService.calculate_totals(1)
        assert meta_totals["paid_total"] == 400
        assert meta_totals["outstanding"] == 700

        ledger_totals = InvoiceService.calculate_totals(2)
        assert ledger_totals["paid_total"] == 500
        assert ledger_totals["outstanding"] == 600


def test_invoice_link_service_reads_active_external_case_map(app, clean_legacy_invoice_db):
    with app.app_context():
        init_db()
        conn = get_db()
        _seed_minimal_invoice(conn, invoice_id=1)
        conn.execute(
            "INSERT OR REPLACE INTO matter (matter_id, our_ref, right_name) VALUES (?, ?, ?)",
            ("MATTER-001", "26PD0103PCT", "Text Text Text"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO matter (matter_id, our_ref, right_name) VALUES (?, ?, ?)",
            ("MATTER-DEL", "26PD0999PCT", "Text Text"),
        )
        conn.execute(
            "INSERT INTO external_invoice_case_map (matter_id, our_ref, external_invoice_id) VALUES (?, ?, ?)",
            ("MATTER-001", "26PD0103PCT", 1),
        )
        conn.execute(
            """
            INSERT INTO external_invoice_case_map (
                matter_id, our_ref, external_invoice_id, is_deleted
            ) VALUES (?, ?, ?, ?)
            """,
            ("MATTER-DEL", "26PD0999PCT", 1, True),
        )
        conn.commit()
        conn.close()

        links = InvoiceLinkService.get_links(1)
        assert [link.matter_id for link in links] == ["MATTER-001"]
        assert InvoiceLinkService.get_invoices_for_case(matter_id="MATTER-001") == [1]
        assert InvoiceLinkService.get_invoices_for_case(matter_id="MATTER-DEL") == []
