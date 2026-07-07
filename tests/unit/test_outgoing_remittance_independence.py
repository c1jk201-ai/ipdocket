import json


def _seed_outgoing_invoice(
    app, *, invoice_id=1, number="INV-OUT-001", total_minor=1100, payment_meta=None
):
    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()
        for tbl in (
            "client_deposit_ledger",
            "bank_transactions",
            "invoice_attachments",
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

        conn.execute(
            "INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute("INSERT INTO clients (id, name) VALUES (1, 'Client A')")
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date, due_date,
                status, billing_status, payment_status, payment_verified,
                subtotal, tax, total, subtotal_minor, tax_minor, total_minor,
                currency, vat_rate, is_outgoing, payment_meta
            ) VALUES (?, 1, 1, ?, '2026-04-01', '2026-04-30',
                'sent', 'sent', 'unpaid', 0,
                1000, 100, 1100, 1000, 100, ?,
                'USD', 10.0, 1, ?)
            """,
            (invoice_id, number, total_minor, payment_meta),
        )
        conn.commit()
        conn.close()

    return invoice_id


def test_outgoing_payment_verification_does_not_require_remittance_proof(
    admin_client, app, clean_legacy_invoice_db
):
    invoice_id = _seed_outgoing_invoice(app, total_minor=110000)
    payment_meta = {
        "currency": "USD",
        "date": "2026-04-10",
        "account_alias": "USD",
        "deposit": "1100",
        "summary": "Client A",
    }

    response = admin_client.post(
        f"/accounting/invoice-system/invoices/{invoice_id}/verify_payment",
        json={"payment_meta": payment_meta},
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        row = conn.execute(
            "SELECT billing_status, payment_status, payment_verified FROM invoices WHERE id=?",
            (invoice_id,),
        ).fetchone()
        conn.close()

    assert (row["billing_status"] or "").strip().lower() == "sent"
    assert (row["payment_status"] or "").strip().lower() == "paid"
    assert int(row["payment_verified"] or 0) == 1


def test_outgoing_deposit_apply_does_not_require_remittance_proof(
    admin_client, app, clean_legacy_invoice_db
):
    invoice_id = _seed_outgoing_invoice(app)

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        conn.execute(
            """
            INSERT INTO client_deposit_ledger (
                business_profile_id, client_id, currency, amount_minor, entry_type, memo
            ) VALUES (1, 1, 'USD', 1100, 'topup', 'seed')
            """
        )
        conn.commit()
        conn.close()

    response = admin_client.post(
        f"/accounting/invoice-system/invoices/{invoice_id}/deposit/apply",
        data={"amount": "", "memo": ""},
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    assert "#sec-remittance-proof" not in (response.headers.get("Location") or "")

    with app.app_context():
        conn = get_db()
        row = conn.execute(
            "SELECT payment_status, payment_verified FROM invoices WHERE id=?",
            (invoice_id,),
        ).fetchone()
        applied = conn.execute(
            """
            SELECT COALESCE(SUM(amount_minor), 0) AS amount
            FROM client_deposit_ledger
            WHERE related_invoice_id=? AND entry_type IN ('apply', 'cancel_apply')
            """,
            (invoice_id,),
        ).fetchone()
        conn.close()

    assert (row["payment_status"] or "").strip().lower() == "paid"
    assert int(row["payment_verified"] or 0) == 1
    assert int(applied["amount"] or 0) == -1100


def test_bank_activity_tax_invoice_sync_does_not_require_remittance_proof(
    admin_client, app, clean_legacy_invoice_db
):
    invoice_id = _seed_outgoing_invoice(app, payment_meta=json.dumps({"tid": "T-OUT-1"}))

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        conn.execute(
            """
            INSERT INTO bank_transactions (
                tid, acc_in, acc_out, memo, created_at, updated_at
            ) VALUES ('T-OUT-1', 1100, 0, 'INV:INV-OUT-001', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )
        conn.commit()
        conn.close()

    response = admin_client.post(
        "/accounting/invoice-system/bank_activity/tax_invoice",
        json={"tid": "T-OUT-1", "issued": True},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["invoiceUpdated"] is True

    with app.app_context():
        conn = get_db()
        row = conn.execute(
            """
            SELECT status, billing_status, tax_issued_at, tax_issue_source
            FROM invoices
            WHERE id=?
            """,
            (invoice_id,),
        ).fetchone()
        conn.close()

    assert (row["status"] or "").strip().lower() == "tax_issued"
    assert (row["billing_status"] or "").strip().lower() == "tax_issued"
    assert row["tax_issued_at"]
    assert row["tax_issue_source"] == "bank_activity"
