from app.blueprints.billing_invoices.db import get_db, init_db
from app.blueprints.billing_invoices.routes.case_matching import score_invoice_case_recommendation


def _seed_case_matching_recommendation(app):
    with app.app_context():
        init_db()
        conn = get_db()
        for table_name in (
            "external_invoice_case_map",
            "line_items",
            "invoices",
            "clients",
            "business_profile",
        ):
            try:
                conn.execute(f"DELETE FROM {table_name}")
            except Exception:
                conn.rollback()
        for matter_id in ("MAT-REC-1", "MAT-OTHER-1"):
            try:
                conn.execute("DELETE FROM case_flat_index WHERE matter_id=?", (matter_id,))
                conn.execute("DELETE FROM matter WHERE matter_id=?", (matter_id,))
            except Exception:
                conn.rollback()

        conn.execute(
            """
            INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no)
            VALUES (1, 'BP1', 'USD', 10.0, 1)
            """
        )
        conn.execute("INSERT INTO clients (id, name) VALUES (1, 'Text')")
        conn.execute("INSERT INTO clients (id, name) VALUES (2, 'Text')")
        conn.execute(
            """
            INSERT INTO matter (matter_id, our_ref, right_name, retained_at)
            VALUES (?, ?, ?, ?)
            """,
            ("MAT-REC-1", "25AA0001US", "Text Text", "2026-03-01"),
        )
        conn.execute(
            """
            INSERT INTO matter (matter_id, our_ref, right_name, retained_at)
            VALUES (?, ?, ?, ?)
            """,
            ("MAT-OTHER-1", "25AA9999US", "Text Text", "2026-03-01"),
        )
        conn.execute(
            """
            INSERT INTO case_flat_index (matter_id, client_name, search_text)
            VALUES (?, ?, ?)
            """,
            ("MAT-REC-1", "Text", "25AA0001US Text Text Text"),
        )
        conn.execute(
            """
            INSERT INTO case_flat_index (matter_id, client_name, search_text)
            VALUES (?, ?, ?)
            """,
            ("MAT-OTHER-1", "Text", "25AA9999US Text Text Text"),
        )
        invoice_rows = [
            (
                100,
                1,
                "INV-20260310-0100",
                "2026-03-10",
                "25AA0001US",
                "",
                "",
            ),
            (
                101,
                1,
                "INV-20260310-0101",
                "2026-03-10",
                "",
                "",
                "",
            ),
            (
                102,
                2,
                "INV-20260310-0102",
                "2026-03-10",
                "25AA9999US",
                "",
                "",
            ),
        ]
        for row in invoice_rows:
            conn.execute(
                """
                INSERT INTO invoices (
                    id, client_id, business_profile_id, number, issue_date,
                    status, billing_status, payment_status, currency, total_minor,
                    internal_reference, ipm_case_id, ipm_case_ref
                ) VALUES (?, ?, 1, ?, ?, 'sent', 'sent', 'unpaid', 'USD', 110000, ?, ?, ?)
                """,
                row,
            )
        conn.commit()
        conn.close()


def test_score_invoice_case_recommendation_uses_ref_anchor_not_client_only():
    good = score_invoice_case_recommendation(
        invoice={
            "id": 1,
            "number": "INV-1",
            "internal_reference": "25AA0001US",
            "client_name": "Text",
            "issue_date": "2026-03-10",
        },
        case={
            "matter_id": "MAT-REC-1",
            "our_ref": "25AA0001US",
            "right_name": "Text Text",
            "client_name": "Text",
            "retained_at": "2026-03-01",
        },
    )
    client_only = score_invoice_case_recommendation(
        invoice={
            "id": 2,
            "number": "INV-2",
            "internal_reference": "",
            "client_name": "Text",
            "issue_date": "2026-03-10",
        },
        case={
            "matter_id": "MAT-REC-1",
            "our_ref": "25AA0001US",
            "right_name": "Text Text",
            "client_name": "Text",
            "retained_at": "2026-03-01",
        },
    )

    assert good["recommended"] is True
    assert "Internal match" in good["reasons"]
    assert client_only["recommended"] is False


def test_score_invoice_case_recommendation_promotes_client_identity_with_title_support():
    with_same_client = score_invoice_case_recommendation(
        invoice={
            "id": 3,
            "number": "INV-3",
            "internal_reference": "",
            "client_name": "Text",
            "line_item_text": "Text / Text / Text Text",
            "issue_date": "2026-03-10",
        },
        case={
            "matter_id": "MAT-REC-1",
            "our_ref": "25AA0001US",
            "right_name": "Text Text",
            "client_name": "Text",
            "retained_at": "2026-03-01",
        },
    )
    without_same_client = score_invoice_case_recommendation(
        invoice={
            "id": 4,
            "number": "INV-4",
            "internal_reference": "",
            "client_name": "Unrelated Client",
            "line_item_text": "Text / Text / Text Text",
            "issue_date": "2026-03-10",
        },
        case={
            "matter_id": "MAT-REC-1",
            "our_ref": "25AA0001US",
            "right_name": "Text Text",
            "client_name": "Text",
            "retained_at": "2026-03-01",
        },
    )

    assert with_same_client["recommended"] is True
    assert "Title contains" in with_same_client["reasons"]
    assert without_same_client["recommended"] is False


def test_matching_invoices_filters_by_recommended_case(admin_client, app):
    _seed_case_matching_recommendation(app)

    response = admin_client.get(
        "/accounting/invoice-system/case-matching/invoices"
        "?recommend_case_id=MAT-REC-1&page=1&perPage=15"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 1
    assert [item["id"] for item in payload["items"]] == [100]
    assert payload["items"][0]["recommended"] is True
    assert payload["items"][0]["recommend_score"] >= 90


def test_matching_cases_filters_by_recommended_invoice(admin_client, app):
    _seed_case_matching_recommendation(app)

    response = admin_client.get(
        "/accounting/invoice-system/case-matching/cases"
        "Newrecommend_invoice_id=100&unmatchedOnly=0&page=1&perPage=15"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 1
    assert [item["matter_id"] for item in payload["items"]] == ["MAT-REC-1"]
    assert payload["items"][0]["recommended"] is True
    assert payload["items"][0]["recommend_score"] >= 90
