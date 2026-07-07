import uuid

from app.blueprints.billing_invoices.db import get_db, init_db


def _seed_invoice(
    *,
    status: str,
    billing_status: str,
    payment_status: str,
    client_name: str | None = None,
    invoice_number: str | None = None,
) -> tuple[int, int, int]:
    init_db()
    conn = get_db()
    try:
        suffix = uuid.uuid4().hex[:8]
        bp_name = f"BP-{suffix}"
        client_name = client_name or f"Aging Client {suffix}"
        client_email = f"aging-{suffix}@example.com"
        conn.execute(
            "INSERT INTO business_profile (name, currency) VALUES (?, ?)",
            (bp_name, "USD"),
        )
        bp_id = conn.execute("SELECT id FROM business_profile WHERE name=?", (bp_name,)).fetchone()[
            0
        ]
        conn.execute(
            "INSERT INTO clients (name, email) VALUES (?, ?)",
            (client_name, client_email),
        )
        client_id = conn.execute("SELECT id FROM clients WHERE name=?", (client_name,)).fetchone()[
            0
        ]
        invoice_number = invoice_number or f"AGING-INV-{uuid.uuid4().hex[:8]}"
        conn.execute(
            """
            INSERT INTO invoices (
                client_id, business_profile_id, number, issue_date, status,
                billing_status, payment_status, subtotal, tax, total, currency
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                client_id,
                bp_id,
                invoice_number,
                "2026-01-01",
                status,
                billing_status,
                payment_status,
                1000,
                100,
                1100,
                "USD",
            ),
        )
        invoice_id = conn.execute(
            "SELECT id FROM invoices WHERE number=?",
            (invoice_number,),
        ).fetchone()[0]
        conn.commit()
        return invoice_id, client_id, bp_id
    finally:
        conn.close()


def _link_invoice_to_matter(invoice_id: int, *, matter_id: str, our_ref: str) -> None:
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO matter (matter_id, our_ref, right_name) VALUES (?, ?, ?)",
            (matter_id, our_ref, "Aging Test Matter"),
        )
        conn.execute(
            """
            INSERT INTO external_invoice_case_map (matter_id, our_ref, external_invoice_id)
            VALUES (?, ?, ?)
            """,
            (matter_id, our_ref, invoice_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_mark_pre_overdue_redirect_and_update(admin_client, db_session):
    invoice_id, client_id, bp_id = _seed_invoice(
        status="sent",
        billing_status="sent",
        payment_status="unpaid",
    )

    resp = admin_client.post(
        f"/accounting/invoice-system/aging/aging/mark_pre_overdue/{invoice_id}",
        data={
            "client_id": client_id,
            "currency": "USD",
            "business_profile_id": bp_id,
            "as_of": "2026-01-31",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert "/accounting/invoice-system/aging/aging/details" in (resp.headers.get("Location") or "")

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status, billing_status FROM invoices WHERE id=?",
            (invoice_id,),
        ).fetchone()
        assert row[0] == "pre_overdue"
        assert row[1] == "pre_overdue"
    finally:
        conn.close()


def test_mark_pre_overdue_does_not_change_paid_invoice(admin_client, db_session):
    invoice_id, client_id, bp_id = _seed_invoice(
        status="paid",
        billing_status="sent",
        payment_status="paid",
    )

    resp = admin_client.post(
        f"/accounting/invoice-system/aging/aging/mark_pre_overdue/{invoice_id}",
        data={
            "client_id": client_id,
            "currency": "USD",
            "business_profile_id": bp_id,
            "as_of": "2026-01-31",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 302

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status, billing_status, payment_status FROM invoices WHERE id=?",
            (invoice_id,),
        ).fetchone()
        assert row[0] == "paid"
        assert row[1] == "sent"
        assert row[2] == "paid"
    finally:
        conn.close()


def test_aging_pages_filter_linked_case_overdues(admin_client, db_session):
    linked_invoice_id, _, _ = _seed_invoice(
        status="sent",
        billing_status="sent",
        payment_status="unpaid",
        client_name="Linked Aging Client",
        invoice_number="AGING-LINKED-001",
    )
    _seed_invoice(
        status="sent",
        billing_status="sent",
        payment_status="unpaid",
        client_name="Unlinked Aging Client",
        invoice_number="AGING-UNLINKED-001",
    )
    _link_invoice_to_matter(
        linked_invoice_id,
        matter_id="MATTER-AGING-001",
        our_ref="26PDAGING001",
    )

    resp = admin_client.get(
        "/accounting/invoice-system/aging/aging"
        "?as_of=2026-02-15&overdue_only=1&case_linked=linked"
    )

    assert resp.status_code == 200
    html = resp.data.decode("utf-8", errors="replace")
    assert "Linked Aging Client" in html
    assert "Unlinked Aging Client" not in html

    resp = admin_client.get(
        "/accounting/invoice-system/aging/aging/invoices"
        "?as_of=2026-02-15&overdue_only=1&case_linked=linked"
    )

    assert resp.status_code == 200
    html = resp.data.decode("utf-8", errors="replace")
    assert "AGING-LINKED-001" in html
    assert "AGING-UNLINKED-001" not in html
