from datetime import date

from app.blueprints.billing_invoices.services import aging_invoices_service as svc


def test_parse_aging_date_accepts_datetime_strings():
    assert svc.parse_aging_date("2026-01-05 13:21:55") == date(2026, 1, 5)
    assert svc.parse_aging_date("2026-01-05T23:59:59Z") == date(2026, 1, 5)
    assert svc.parse_aging_date("not-a-date") is None


def test_parse_aging_deposit_info_parses_usd_decimal_without_crash():
    dep, dep_date = svc.parse_aging_deposit_info(
        {"deposit": "1,234.9", "date": "2026-01-01"},
        "USD",
    )
    assert dep == 1234.0
    assert dep_date == "2026-01-01"


def test_build_aging_invoices_result_filters_zero_outstanding_and_handles_datetime_due(monkeypatch):
    raw_rows = [
        {
            "id": 1,
            "client_name": "Client A",
            "number": "INV-1",
            "currency": "USD",
            "admin_total": 0,
            "foreign_total": 0,
            "payment_meta": '{"deposit":"1000"}',
            "total": 1000,
            "due_date": "2026-01-02 00:00:00",
            "issue_date": "2026-01-01",
            "billing_status": "sent",
        },
        {
            "id": 2,
            "client_name": "Client B",
            "number": "INV-2",
            "currency": "USD",
            "admin_total": 0,
            "foreign_total": 0,
            "payment_meta": '{"deposit":"200.00","time":"2026-01-01 09:00:00"}',
            "total": 1000,
            "due_date": "2026-01-10T00:00:00",
            "issue_date": "2026-01-01",
            "billing_status": "pre_overdue",
        },
    ]

    monkeypatch.setattr(svc, "fetch_aging_invoices_rows", lambda **_: raw_rows)
    monkeypatch.setattr(svc, "get_all_business_profiles", lambda: [])
    monkeypatch.setattr(svc, "get_business_profile", lambda _bp_id: None)

    result = svc.build_aging_invoices_result(
        bp_ids=[],
        q="",
        is_compact_q=False,
        as_of_date=date(2026, 1, 5),
        overdue_only=False,
        case_linked="",
        sort_by="issue_date",
    )

    assert [row["id"] for row in result.rows] == [2]
    assert result.rows[0]["outstanding"] == 800.0
    assert result.rows[0]["days_over"] == 1
