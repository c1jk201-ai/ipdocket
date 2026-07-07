import json

from app.blueprints.billing_invoices.db import get_db, init_db
from app.blueprints.billing_invoices.routes import bank_activity
from app.blueprints.billing_invoices.routes.bank_activity import _score_invoice_deposit_recommendation


def _seed_bank_activity_matching_history(app):
    with app.app_context():
        init_db()
        conn = get_db()
        for table_name in (
            "client_deposit_ledger",
            "invoice_case_map",
            "external_invoice_case_map",
            "bank_transactions",
            "line_items",
            "invoices",
            "clients",
            "business_profile",
        ):
            try:
                conn.execute(f"DELETE FROM {table_name}")
            except Exception:
                continue

        conn.execute(
            """
            INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no)
            VALUES (1, 'BP1', 'USD', 10.0, 1)
            """
        )
        conn.execute("INSERT INTO clients (id, name) VALUES (1, 'Acme IP Holdings')")
        conn.execute("INSERT INTO clients (id, name) VALUES (2, 'Unrelated Holdings')")

        history_meta = json.dumps(
            {
                "currency": "USD",
                "date": "2026-03-05",
                "account_alias": "USD operating account",
                "deposit": 100000,
                "summary": "Known Remitter | 123456 | Wire",
                "deposits": [
                    {
                        "tid": "hist-tid-1",
                        "date": "2026-03-05",
                        "account_alias": "USD operating account",
                        "deposit": 100000,
                        "currency": "USD",
                        "summary": "Known Remitter | 123456 | Wire",
                    }
                ],
            },
            ensure_ascii=False,
        )

        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date,
                status, billing_status, payment_status, currency, total_minor, payment_meta
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                10,
                1,
                1,
                "INV-20260305-0001",
                "2026-03-05",
                "sent",
                "sent",
                "paid",
                "USD",
                100000,
                history_meta,
            ),
        )
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date,
                status, billing_status, payment_status, currency, total_minor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                20,
                1,
                1,
                "INV-20260101-0001",
                "2026-01-01",
                "sent",
                "sent",
                "unpaid",
                "USD",
                2200000,
            ),
        )
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date,
                status, billing_status, payment_status, currency, total_minor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                21,
                2,
                1,
                "INV-20260101-0002",
                "2026-01-01",
                "sent",
                "sent",
                "unpaid",
                "USD",
                2200000,
            ),
        )

        tx_rows = [
            (
                "hist-tid-1",
                "004",
                "1234567890",
                "20260305",
                "20260305120000",
                100000,
                0,
                500000,
                "Known Remitter",
                "123456",
                "Wire",
                "INV:INV-20260305-0001",
            ),
            (
                "current-tid-history",
                "004",
                "1234567890",
                "20260310",
                "20260310120000",
                2200000,
                0,
                2700000,
                "Known Remitter",
                "654321",
                "Wire",
                "",
            ),
            (
                "current-tid-other",
                "004",
                "1234567890",
                "20260310",
                "20260310130000",
                2200000,
                0,
                4900000,
                "Zenith Transfer",
                "777777",
                "Wire",
                "",
            ),
        ]
        for row in tx_rows:
            conn.execute(
                """
                INSERT INTO bank_transactions (
                    tid, bank_code, account_number, trdate, trdt,
                    acc_in, acc_out, balance, remark1, remark2, remark3, memo
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )

        conn.commit()
        conn.close()


def test_recommendation_score_prefers_exact_amount_date_and_name():
    recommend = _score_invoice_deposit_recommendation(
        invoice={
            "id": 1,
            "client_id": 1,
            "issue_date": "2026-03-10",
            "total": 3300000,
            "remaining": 3300000,
            "client_name": "Text Text",
        },
        deposit={
            "tid": "t1",
            "trdt": "20260313095704",
            "trdate": "20260313",
            "accIn": 3300000,
            "payer_name": "Text Text",
        },
    )

    assert recommend["recommended"] is True
    assert recommend["score"] >= 90
    assert "Amount match" in recommend["reasons"]
    assert "Issued within 3 days" in recommend["reasons"]
    assert "Client name match" in recommend["reasons"]


def test_recommendation_score_uses_split_deposit_remaining_amount():
    invoice = {
        "id": 9,
        "client_id": 2,
        "issue_date": "2026-03-01",
        "total": 536,
        "remaining": 536,
        "currency": "USD",
        "client_name": "USD Client",
    }
    split_deposit = {
        "tid": "usd-split",
        "trdt": "20260302120000",
        "trdate": "20260302",
        "accIn": 1096,
        "matched_amount": 560,
        "remainingAccIn": 536,
        "match_state": "partial",
        "payer_name": "USD Client",
    }

    recommend = _score_invoice_deposit_recommendation(
        invoice=invoice,
        deposit=split_deposit,
    )
    old_full_amount = _score_invoice_deposit_recommendation(
        invoice=invoice,
        deposit={
            key: value
            for key, value in split_deposit.items()
            if key not in {"matched_amount", "remainingAccIn", "match_state"}
        },
    )

    assert recommend["recommended"] is True
    assert "Amount match" in recommend["reasons"]
    assert old_full_amount["recommended"] is False


def test_recommendation_still_allows_common_name_mismatch_when_amount_and_date_fit():
    recommend = _score_invoice_deposit_recommendation(
        invoice={
            "id": 2,
            "client_id": 2,
            "issue_date": "2026-03-10",
            "total": 2200000,
            "remaining": 2200000,
            "client_name": "Acme IP Holdings",
        },
        deposit={
            "tid": "t2",
            "trdt": "20260311120000",
            "trdate": "20260311",
            "accIn": 2200000,
            "payer_name": "Zenith Remitter",
        },
    )

    assert recommend["recommended"] is True
    assert recommend["score"] >= 60
    assert "Amount match" in recommend["reasons"]
    assert "Issued within 3 days" in recommend["reasons"]


def test_recommendation_rejects_large_amount_gap_without_name_support():
    recommend = _score_invoice_deposit_recommendation(
        invoice={
            "id": 3,
            "client_id": 3,
            "issue_date": "2026-03-10",
            "total": 2200000,
            "remaining": 2200000,
            "client_name": "Acme IP Holdings",
        },
        deposit={
            "tid": "t3",
            "trdt": "20260312090000",
            "trdate": "20260312",
            "accIn": 300000,
            "payer_name": "Zenith Remitter",
        },
    )

    assert recommend["recommended"] is False
    assert recommend["score"] < 48


def test_recommendation_history_bonus_promotes_known_payer_for_client():
    no_history = _score_invoice_deposit_recommendation(
        invoice={
            "id": 4,
            "client_id": 1,
            "issue_date": "2026-01-01",
            "total": 2200000,
            "remaining": 2200000,
            "client_name": "Acme IP Holdings",
        },
        deposit={
            "tid": "t4",
            "trdt": "20260310120000",
            "trdate": "20260310",
            "accIn": 2200000,
            "payer_name": "Known Remitter",
        },
    )
    with_history = _score_invoice_deposit_recommendation(
        invoice={
            "id": 4,
            "client_id": 1,
            "issue_date": "2026-01-01",
            "total": 2200000,
            "remaining": 2200000,
            "client_name": "Acme IP Holdings",
        },
        deposit={
            "tid": "t4",
            "trdt": "20260310120000",
            "trdate": "20260310",
            "accIn": 2200000,
            "payer_name": "Known Remitter",
        },
        history_by_client={1: [{"payer_name": "Known Remitter", "count": 2}]},
    )

    assert no_history["recommended"] is False
    assert no_history["score"] == 45
    assert with_history["recommended"] is True
    assert with_history["score"] > no_history["score"]
    assert "Historical payer match (2 times)" in with_history["reasons"]


def test_matching_invoices_uses_historical_client_payer_matches(admin_client, app):
    _seed_bank_activity_matching_history(app)

    response = admin_client.get(
        "/accounting/invoice-system/bank_activity/matching/invoices"
        "?date_from=2026-01-01"
        "&date_to=2026-03-31"
        "&bpIds=1"
        "&recommend_tid=current-tid-history"
        "&page=1"
        "&perPage=15"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 1
    assert [item["id"] for item in payload["items"]] == [20]
    assert "Historical payer match" in payload["items"][0]["recommend_reasons"]


def test_matching_deposits_uses_historical_client_payer_matches(admin_client, app):
    _seed_bank_activity_matching_history(app)

    response = admin_client.get(
        "/accounting/invoice-system/bank_activity/matching/deposits"
        "?sdate=20260301"
        "&edate=20260331"
        "&accounts=004%7C1234567890"
        "&memoMode=empty"
        "&recommend_invoice_id=20"
        "&page=1"
        "&perPage=15"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 1
    assert [item["tid"] for item in payload["items"]] == ["current-tid-history"]
    assert "Historical payer match" in payload["items"][0]["recommend_reasons"]


def test_matching_invoices_and_profiles_filter_by_currency(admin_client, app):
    with app.app_context():
        init_db()
        conn = get_db()
        for table_name in (
            "client_deposit_ledger",
            "invoice_case_map",
            "external_invoice_case_map",
            "bank_transactions",
            "line_items",
            "invoices",
            "clients",
            "business_profile",
        ):
            try:
                conn.execute(f"DELETE FROM {table_name}")
            except Exception:
                continue

        conn.execute(
            """
            INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no)
            VALUES (1, 'EUR BP', 'EUR', 10.0, 1)
            """
        )
        conn.execute(
            """
            INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no)
            VALUES (2, 'USD BP', 'USD', 0.0, 1)
            """
        )
        conn.execute("INSERT INTO clients (id, name) VALUES (1, 'USD Client')")
        conn.execute("INSERT INTO clients (id, name) VALUES (2, 'USD Client')")
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date,
                status, billing_status, payment_status, currency, total_minor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (30, 1, 1, "INV-EUR", "2026-03-01", "sent", "sent", "unpaid", "EUR", 100000),
        )
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date,
                status, billing_status, payment_status, currency, total_minor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (31, 2, 2, "INV-USD", "2026-03-01", "sent", "sent", "unpaid", "USD", 123400),
        )
        conn.commit()

    default_response = admin_client.get(
        "/accounting/invoice-system/bank_activity/matching/invoices"
        "?date_from=2026-03-01&date_to=2026-03-31&page=1&perPage=15"
    )
    assert default_response.status_code == 200
    default_payload = default_response.get_json()
    assert [item["number"] for item in default_payload["items"]] == ["INV-USD"]

    usd_response = admin_client.get(
        "/accounting/invoice-system/bank_activity/matching/invoices"
        "?currency=USD&date_from=2026-03-01&date_to=2026-03-31&page=1&perPage=15"
    )
    assert usd_response.status_code == 200
    usd_payload = usd_response.get_json()
    assert usd_payload["currency"] == "USD"
    assert [item["number"] for item in usd_payload["items"]] == ["INV-USD"]
    assert usd_payload["items"][0]["total"] == 1234

    bp_response = admin_client.get(
        "/accounting/invoice-system/bank_activity/matching/biz_profiles?currency=USD"
    )
    assert bp_response.status_code == 200
    bp_payload = bp_response.get_json()
    assert bp_payload["currency"] == "USD"
    assert [item["name"] for item in bp_payload["items"]] == ["USD BP"]


def test_bank_activity_currency_pages_render(admin_client):
    page_response = admin_client.get("/accounting/invoice-system/bank_activity/page?currency=USD")
    assert page_response.status_code == 200
    page_html = page_response.get_data(as_text=True)
    assert "USD" in page_html
    assert "bank_activity-currency-switch" in page_html
    assert 'class="btn-group" role="group" aria-label="Text Text"' not in page_html

    matching_response = admin_client.get(
        "/accounting/invoice-system/bank_activity/matching?currency=USD"
    )
    assert matching_response.status_code == 200
    html = matching_response.get_data(as_text=True)
    assert "USD Invoice(Issued)" in html
    assert "Business profile (USD)" in html
    assert 'const MATCH_CURRENCY = "USD";' in html
    assert "/force_paid" in html


def test_bank_activity_db_accounts_filter_usd_account(admin_client, app):
    with app.app_context():
        init_db()
        conn = get_db()
        try:
            conn.execute("DELETE FROM bank_transactions")
        except Exception:
            conn.rollback()
        tx_rows = [
            ("usd-local", "0020", "1081701443823", "USD", "20260302", "20260302120000", 1234),
        ]
        for row in tx_rows:
            conn.execute(
                """
                INSERT INTO bank_transactions (
                    tid, bank_code, account_number, currency, trdate, trdt, acc_in
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
        conn.commit()

    response = admin_client.get(
        "/accounting/invoice-system/bank_activity/db_accounts"
        "?currency=USD&sdate=20260301&edate=20260331&depositsOnly=1"
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["items"] == [
        {"bankCode": "0020", "accountNumber": "1081701443823", "currency": "USD"}
    ]


def test_manual_usd_transaction_is_saved_as_local_account_and_deposit(admin_client, app):
    with app.app_context():
        init_db()
        conn = get_db()
        try:
            conn.execute("DELETE FROM bank_transactions")
            conn.commit()
        finally:
            conn.close()

    response = admin_client.post(
        "/accounting/invoice-system/bank_activity/manual_transaction",
        json={
            "currency": "USD",
            "transactionDate": "2026-03-02",
            "accountNumber": "operating-usd",
            "accountName": "US operating account",
            "accIn": 1234,
            "balance": 5000,
            "payerName": "USD Client",
            "reference": "wire-100",
        },
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    tid = payload["tid"]

    with app.app_context():
        conn = get_db()
        row = conn.execute(
            """
            SELECT tid, bank_code, account_number, account_name, currency, source_provider,
                   acc_in, acc_out, balance, remark1, remark2
            FROM bank_transactions
            WHERE tid=?
            """,
            (tid,),
        ).fetchone()
        conn.close()
    assert row is not None
    assert row[1] == "MANUAL"
    assert row[2] == "operating-usd"
    assert row[3] == "US operating account"
    assert row[4] == "USD"
    assert row[5] == "manual"
    assert row[6] == 123400
    assert row[7] == 0
    assert row[8] == 500000
    assert row[9] == "USD Client"
    assert row[10] == "wire-100"

    accounts_response = admin_client.get(
        "/accounting/invoice-system/bank_activity/accounts?currency=USD&provider=manual"
    )
    assert accounts_response.status_code == 200
    accounts_payload = accounts_response.get_json()
    assert accounts_payload["provider"] == "manual"
    assert accounts_payload["active"] == [
        {
            "accountName": "US operating account",
            "accountNumber": "operating-usd",
            "bankCode": "MANUAL",
            "currency": "USD",
            "sourceProvider": "manual",
            "state": 1,
        }
    ]

    deposits_response = admin_client.get(
        "/accounting/invoice-system/bank_activity/matching/deposits"
        "?currency=USD&sdate=20260301&edate=20260331&accounts=MANUAL%7Coperating-usd"
        "&memoMode=all&page=1&perPage=15"
    )
    assert deposits_response.status_code == 200
    deposits_payload = deposits_response.get_json()
    assert deposits_payload["total"] == 1
    assert deposits_payload["items"][0]["tid"] == tid
    assert deposits_payload["items"][0]["accIn"] == 1234


def test_usd_bank_activity_match_saves_major_amount_and_verifies(admin_client, app):
    with app.app_context():
        init_db()
        conn = get_db()
        for table_name in (
            "client_deposit_ledger",
            "invoice_case_map",
            "external_invoice_case_map",
            "bank_transactions",
            "line_items",
            "invoices",
            "clients",
            "business_profile",
        ):
            try:
                conn.execute(f"DELETE FROM {table_name}")
            except Exception:
                continue

        conn.execute(
            """
            INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no)
            VALUES (2, 'USD BP', 'USD', 0.0, 1)
            """
        )
        conn.execute("INSERT INTO clients (id, name) VALUES (2, 'USD Client')")
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date,
                status, billing_status, payment_status, currency, total_minor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (41, 2, 2, "INV-USD-PAY", "2026-03-01", "sent", "sent", "unpaid", "USD", 123400),
        )
        conn.execute(
            """
            INSERT INTO bank_transactions (
                tid, bank_code, account_number, trdate, trdt,
                acc_in, acc_out, balance, remark1, remark2, remark3, memo
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "usd-tid-1",
                "004",
                "99887766",
                "20260302",
                "20260302120000",
                123400,
                0,
                123400,
                "USD Client",
                "",
                "",
                "",
            ),
        )
        conn.commit()

    detail_response = admin_client.get("/accounting/invoice-system/bank_activity/matching/invoice/41")
    assert detail_response.status_code == 200
    assert "USD" in detail_response.get_data(as_text=True)

    save_response = admin_client.post(
        "/accounting/invoice-system/invoices/41/save_payment_meta",
        json={
            "payment_meta": {
                "currency": "USD",
                "date": "2026-03-02",
                "account_alias": "USD account",
                "deposit": 1234,
                "summary": "USD Client",
            },
            "append": True,
            "tid": "usd-tid-1",
        },
    )
    assert save_response.status_code == 200
    saved_meta = save_response.get_json()["payment_meta"]
    assert saved_meta["currency"] == "USD"
    assert saved_meta["deposit"] == 1234
    assert saved_meta["deposits"][0]["deposit"] == 1234

    verify_response = admin_client.post(
        "/accounting/invoice-system/invoices/41/verify_payment",
        json={"payment_meta": saved_meta},
    )
    assert verify_response.status_code == 200
    verify_payload = verify_response.get_json()
    assert verify_payload["ok"] is True
    assert verify_payload["status"] == "paid"


def test_usd_bank_activity_split_deposit_can_pay_multiple_invoices(admin_client, app):
    with app.app_context():
        init_db()
        conn = get_db()
        for table_name in (
            "client_deposit_ledger",
            "invoice_case_map",
            "external_invoice_case_map",
            "bank_transactions",
            "line_items",
            "invoices",
            "clients",
            "business_profile",
        ):
            try:
                conn.execute(f"DELETE FROM {table_name}")
            except Exception:
                continue

        conn.execute(
            """
            INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no)
            VALUES (2, 'USD BP', 'USD', 0.0, 1)
            """
        )
        conn.execute("INSERT INTO clients (id, name) VALUES (2, 'USD Client')")
        for invoice_id, number, total_minor in (
            (51, "INV-USD-SPLIT-A", 56000),
            (52, "INV-USD-SPLIT-B", 53600),
            (53, "INV-USD-SPLIT-C", 100),
        ):
            conn.execute(
                """
                INSERT INTO invoices (
                    id, client_id, business_profile_id, number, issue_date,
                    status, billing_status, payment_status, currency, total_minor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invoice_id,
                    2,
                    2,
                    number,
                    "2026-03-01",
                    "sent",
                    "sent",
                    "unpaid",
                    "USD",
                    total_minor,
                ),
            )
        conn.execute(
            """
            INSERT INTO bank_transactions (
                tid, bank_code, account_number, trdate, trdt,
                acc_in, acc_out, balance, remark1, remark2, remark3, memo
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "usd-split-tid",
                "0020",
                "1081701443823",
                "20260302",
                "20260302120000",
                109600,
                0,
                109600,
                "USD Client",
                "",
                "",
                "",
            ),
        )
        conn.commit()

    first_save = admin_client.post(
        "/accounting/invoice-system/invoices/51/save_payment_meta",
        json={
            "payment_meta": {
                "currency": "USD",
                "date": "2026-03-02",
                "account_alias": "USD account",
                "deposit": 560,
                "summary": "USD Client",
                "match_mode": "deposit_split_to_invoices",
            },
            "append": True,
            "tid": "usd-split-tid",
            "allow_multi_invoice": True,
        },
    )
    assert first_save.status_code == 200
    first_meta = first_save.get_json()["payment_meta"]
    first_verify = admin_client.post(
        "/accounting/invoice-system/invoices/51/verify_payment",
        json={"payment_meta": first_meta},
    )
    assert first_verify.status_code == 200
    assert first_verify.get_json()["ok"] is True

    with app.app_context():
        conn = get_db()
        conn.execute(
            "UPDATE bank_transactions SET memo=? WHERE tid=?",
            ("INV:INV-USD-SPLIT-A", "usd-split-tid"),
        )
        conn.commit()

    rejected = admin_client.post(
        "/accounting/invoice-system/invoices/52/save_payment_meta",
        json={
            "payment_meta": {
                "currency": "USD",
                "date": "2026-03-02",
                "account_alias": "USD account",
                "deposit": 536,
                "summary": "USD Client",
            },
            "append": True,
            "tid": "usd-split-tid",
        },
    )
    assert rejected.status_code == 400

    second_save = admin_client.post(
        "/accounting/invoice-system/invoices/52/save_payment_meta",
        json={
            "payment_meta": {
                "currency": "USD",
                "date": "2026-03-02",
                "account_alias": "USD account",
                "deposit": 536,
                "summary": "USD Client",
                "source_deposit": 1096,
                "match_mode": "deposit_split_to_invoices",
            },
            "append": True,
            "tid": "usd-split-tid",
            "allow_multi_invoice": True,
        },
    )
    assert second_save.status_code == 200
    second_meta = second_save.get_json()["payment_meta"]
    assert second_meta["deposit"] == 536

    second_verify = admin_client.post(
        "/accounting/invoice-system/invoices/52/verify_payment",
        json={"payment_meta": second_meta},
    )
    assert second_verify.status_code == 200
    assert second_verify.get_json()["ok"] is True

    over_allocated = admin_client.post(
        "/accounting/invoice-system/invoices/53/save_payment_meta",
        json={
            "payment_meta": {
                "currency": "USD",
                "date": "2026-03-02",
                "account_alias": "USD account",
                "deposit": 1,
                "summary": "USD Client",
                "source_deposit": 1096,
                "match_mode": "deposit_split_to_invoices",
            },
            "append": True,
            "tid": "usd-split-tid",
            "allow_multi_invoice": True,
        },
    )
    assert over_allocated.status_code == 400
    assert "Deposit allocation exceeds available amount" in over_allocated.get_json()["error"]


def test_matching_deposits_keeps_partially_allocated_split_deposit_visible(admin_client, app):
    with app.app_context():
        init_db()
        conn = get_db()
        for table_name in (
            "client_deposit_ledger",
            "invoice_case_map",
            "external_invoice_case_map",
            "bank_transactions",
            "line_items",
            "invoices",
            "clients",
            "business_profile",
        ):
            try:
                conn.execute(f"DELETE FROM {table_name}")
            except Exception:
                continue

        conn.execute(
            """
            INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no)
            VALUES (2, 'USD BP', 'USD', 0.0, 1)
            """
        )
        conn.execute("INSERT INTO clients (id, name) VALUES (2, 'USD Client')")
        payment_meta = json.dumps(
            {
                "currency": "USD",
                "deposit": 560,
                "deposits": [
                    {
                        "tid": "usd-partial-tid",
                        "currency": "USD",
                        "deposit": 560,
                        "summary": "USD Client",
                    }
                ],
            },
            ensure_ascii=False,
        )
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date,
                status, billing_status, payment_status, currency, total_minor, payment_meta
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                61,
                2,
                2,
                "INV-USD-PARTIAL-A",
                "2026-03-01",
                "sent",
                "sent",
                "paid",
                "USD",
                56000,
                payment_meta,
            ),
        )
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date,
                status, billing_status, payment_status, currency, total_minor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                62,
                2,
                2,
                "INV-USD-PARTIAL-B",
                "2026-03-01",
                "sent",
                "sent",
                "unpaid",
                "USD",
                53600,
            ),
        )
        conn.execute(
            """
            INSERT INTO bank_transactions (
                tid, bank_code, account_number, trdate, trdt,
                acc_in, acc_out, balance, remark1, remark2, remark3, memo
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "usd-partial-tid",
                "0020",
                "1081701443823",
                "20260302",
                "20260302120000",
                109600,
                0,
                109600,
                "USD Client",
                "",
                "",
                "INV:INV-USD-PARTIAL-A",
            ),
        )
        conn.commit()

    response = admin_client.get(
        "/accounting/invoice-system/bank_activity/matching/deposits"
        "?sdate=20260301&edate=20260331"
        "&accounts=0020%7C1081701443823"
        "&memoMode=empty&currency=USD"
    )
    assert response.status_code == 200
    payload = response.get_json()
    item = next(i for i in payload["items"] if i["tid"] == "usd-partial-tid")
    assert item["match_state"] == "partial"
    assert item["remainingAccIn"] == 536

    detail_search_response = admin_client.get(
        "/accounting/invoice-system/bank_activity/local_search"
        "?sdate=20260301&edate=20260331"
        "&accounts=0020%7C1081701443823"
        "&tradeType=I&excludeMatched=0&currency=USD"
    )
    assert detail_search_response.status_code == 200
    detail_payload = detail_search_response.get_json()
    detail_item = next(i for i in detail_payload["list"] if i["tid"] == "usd-partial-tid")
    assert detail_item["match_state"] == "partial"
    assert detail_item["remainingAccIn"] == 536

    recommend_response = admin_client.get(
        "/accounting/invoice-system/bank_activity/matching/deposits"
        "?sdate=20260301&edate=20260331"
        "&accounts=0020%7C1081701443823"
        "&memoMode=empty&currency=USD"
        "&recommend_invoice_id=62"
    )
    assert recommend_response.status_code == 200
    recommend_payload = recommend_response.get_json()
    recommended_item = next(i for i in recommend_payload["items"] if i["tid"] == "usd-partial-tid")
    assert recommended_item["recommended"] is True
    assert "Amount match" in recommended_item["recommend_reasons"]


def test_deleted_invoice_allocation_does_not_consume_split_deposit(admin_client, app):
    with app.app_context():
        init_db()
        conn = get_db()
        for table_name in (
            "client_deposit_ledger",
            "invoice_case_map",
            "external_invoice_case_map",
            "bank_transactions",
            "line_items",
            "invoices",
            "clients",
            "business_profile",
        ):
            try:
                conn.execute(f"DELETE FROM {table_name}")
            except Exception:
                continue

        conn.execute(
            """
            INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no)
            VALUES (2, 'USD BP', 'USD', 0.0, 1)
            """
        )
        conn.execute("INSERT INTO clients (id, name) VALUES (2, 'USD Client')")
        deleted_payment_meta = json.dumps(
            {
                "currency": "USD",
                "deposit": 560,
                "deposits": [
                    {
                        "tid": "usd-deleted-tid",
                        "currency": "USD",
                        "deposit": 560,
                        "summary": "Deleted allocation",
                    }
                ],
            },
            ensure_ascii=False,
        )
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date,
                status, billing_status, payment_status, currency, total_minor, payment_meta, is_deleted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                71,
                2,
                2,
                "INV-USD-DELETED-OLD",
                "2026-03-01",
                "sent",
                "sent",
                "paid",
                "USD",
                56000,
                deleted_payment_meta,
                1,
            ),
        )
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date,
                status, billing_status, payment_status, currency, total_minor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                72,
                2,
                2,
                "INV-USD-DELETED-NEW",
                "2026-03-02",
                "sent",
                "sent",
                "unpaid",
                "USD",
                56000,
            ),
        )
        conn.execute(
            """
            INSERT INTO bank_transactions (
                tid, bank_code, account_number, trdate, trdt,
                acc_in, acc_out, balance, remark1, remark2, remark3, memo
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "usd-deleted-tid",
                "0020",
                "1081701443823",
                "20260302",
                "20260302120000",
                56000,
                0,
                56000,
                "USD Client",
                "",
                "",
                "",
            ),
        )
        conn.commit()

    list_response = admin_client.get(
        "/accounting/invoice-system/bank_activity/matching/deposits"
        "?sdate=20260301&edate=20260331"
        "&accounts=0020%7C1081701443823"
        "&memoMode=empty&currency=USD"
    )
    assert list_response.status_code == 200
    item = next(i for i in list_response.get_json()["items"] if i["tid"] == "usd-deleted-tid")
    assert item["match_state"] == "unmatched"
    assert item["remainingAccIn"] == 560

    save_response = admin_client.post(
        "/accounting/invoice-system/invoices/72/save_payment_meta",
        json={
            "payment_meta": {
                "currency": "USD",
                "date": "2026-03-02",
                "account_alias": "USD account",
                "deposit": 560,
                "summary": "USD Client",
            },
            "append": True,
            "tid": "usd-deleted-tid",
        },
    )
    assert save_response.status_code == 200


def test_client_payer_history_index_uses_cache_and_supports_invalidation():
    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return list(self._rows)

    class FakeConn:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params):
            self.calls.append((sql, tuple(params)))
            if "FROM invoices" in sql:
                return FakeResult(
                    [
                        (
                            1,
                            json.dumps(
                                {
                                    "deposits": [
                                        {
                                            "tid": "hist-tid-cache",
                                            "summary": "Text | 111111 | Text",
                                            "deposit": 100000,
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
                )
            if "FROM bank_transactions" in sql:
                return FakeResult([("hist-tid-cache", "Text", "111111", "Text")])
            raise AssertionError(sql)

    conn = FakeConn()
    bank_activity.invalidate_client_payer_history_cache()

    first = bank_activity._load_client_payer_history_index(conn, [1])
    assert len(conn.calls) == 2
    assert first[1][0]["payer_name"] == "Text"

    second = bank_activity._load_client_payer_history_index(conn, [1])
    assert second == first
    assert len(conn.calls) == 2

    bank_activity.invalidate_client_payer_history_cache([1])
    third = bank_activity._load_client_payer_history_index(conn, [1])
    assert third == first
    assert len(conn.calls) == 4
