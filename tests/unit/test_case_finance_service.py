from app.services.billing.case_finance_service import CaseFinanceService, _normalize_invoices


def test_list_ledger_uses_line_items_summary_for_invoice_title():
    invoices = [
        {
            "invoice_id": 101,
            "invoice_no": "INV-101",
            "title": "Long memo text that should not be shown in ledger item column",
            "line_items_summary": "Design filing fee, design official fee, trademark filing fee +1",
            "issue_date": "2026-02-05",
            "due_date": "2026-02-15",
            "status": "PAID",
            "total_minor": 1316400,
            "paid_minor": 1316400,
            "outstanding_minor": 0,
        }
    ]

    ledger = CaseFinanceService.list_ledger("M-CASE-1", invoices=invoices, payables=[])

    assert len(ledger) == 1
    assert ledger[0]["type"] == "INVOICE"
    assert ledger[0]["title"] == invoices[0]["line_items_summary"]


def test_list_ledger_falls_back_to_invoice_title_when_line_items_missing():
    invoices = [
        {
            "invoice_id": 102,
            "invoice_no": "INV-102",
            "title": "Default invoice title",
            "line_items_summary": "",
            "issue_date": "2026-02-05",
            "due_date": "2026-02-15",
            "status": "SENT",
            "total_minor": 100000,
            "paid_minor": 0,
            "outstanding_minor": 100000,
        }
    ]

    ledger = CaseFinanceService.list_ledger("M-CASE-2", invoices=invoices, payables=[])

    assert len(ledger) == 1
    assert ledger[0]["type"] == "INVOICE"
    assert ledger[0]["title"] == "Default invoice title"


def test_normalize_invoices_keeps_billing_and_payment_status_badges():
    invoices = _normalize_invoices(
        [
            {
                "invoice_id": 103,
                "invoice_no": "INV-103",
                "title": "Invoice with badges",
                "issued_at": "2026-03-05",
                "due_at": "2026-03-15",
                "status": "OVERDUE",
                "billing_status": "sent",
                "billing_status_label": "Text",
                "billing_status_pill": "sent",
                "payment_status": "pending",
                "payment_status_label": "Text(Text)",
                "payment_status_pill": "pending",
                "is_overdue": True,
                "total": 50000,
                "paid": 10000,
                "outstanding": 40000,
                "currency": "USD",
            }
        ]
    )

    assert len(invoices) == 1
    invoice = invoices[0]
    assert invoice["billing_status"] == "sent"
    assert invoice["billing_status_label"] == "Text"
    assert invoice["payment_status"] == "pending"
    assert invoice["payment_status_label"] == "Text(Text)"
    assert invoice["is_overdue"] is True
