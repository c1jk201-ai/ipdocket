from app.services.billing import case_invoice_service as case_invoice_service_module


def test_fetch_case_invoices_exposes_split_status_badges(app, monkeypatch):
    with app.app_context():
        app.config["INVOICE_MODULE_VIEW_BASE_URL"] = "/accounting/invoice-system/invoices"

        monkeypatch.setattr(
            case_invoice_service_module,
            "fetch_case_invoice_ids",
            lambda matter_id: [11],
        )
        monkeypatch.setattr(
            case_invoice_service_module.InvoiceService,
            "get_by_id",
            lambda invoice_id: {
                "id": invoice_id,
                "number": "INV-11",
                "issue_date": "2026-03-01",
                "due_date": "2026-03-10",
                "currency": "USD",
                "total_minor": 100000,
                "billing_status": "tax_issued",
                "payment_status": "paid",
                "payment_verified": 1,
                "notes": "Text Text Text",
            },
        )
        monkeypatch.setattr(
            case_invoice_service_module.InvoiceService,
            "get_line_items",
            lambda invoice_id: [],
        )
        monkeypatch.setattr(
            case_invoice_service_module.PaymentService,
            "get_total_paid",
            lambda invoice_id: 100000,
        )

        payload = case_invoice_service_module.fetch_case_invoices("M-CASE-1")

    assert payload["summary"]["outstanding"] == 0
    assert len(payload["invoices"]) == 1
    invoice = payload["invoices"][0]
    assert invoice["status"] == "PAID"
    assert invoice["billing_status"] == "tax_issued"
    assert invoice["billing_status_label"] == "Tax recorded"
    assert invoice["billing_status_pill"] == "tax_issued"
    assert invoice["payment_status"] == "paid"
    assert invoice["payment_status_label"] == "Paid"
    assert invoice["payment_status_pill"] == "paid"
    assert invoice["is_overdue"] is False
    assert invoice["open_url"] == "/accounting/invoice-system/invoices/11"


def test_fetch_case_invoices_derives_badges_from_legacy_status_and_marks_overdue(app, monkeypatch):
    with app.app_context():
        monkeypatch.setattr(
            case_invoice_service_module,
            "fetch_case_invoice_ids",
            lambda matter_id: [22],
        )
        monkeypatch.setattr(
            case_invoice_service_module.InvoiceService,
            "get_by_id",
            lambda invoice_id: {
                "id": invoice_id,
                "number": "INV-22",
                "issue_date": "2026-01-01",
                "due_date": "2000-01-01",
                "currency": "USD",
                "total_minor": 30000,
                "status": "payment_pending",
                "payment_verified": 0,
                "notes": "legacy row",
            },
        )
        monkeypatch.setattr(
            case_invoice_service_module.InvoiceService,
            "get_line_items",
            lambda invoice_id: [],
        )
        monkeypatch.setattr(
            case_invoice_service_module.PaymentService,
            "get_total_paid",
            lambda invoice_id: 0,
        )

        payload = case_invoice_service_module.fetch_case_invoices("M-CASE-2")

    assert len(payload["invoices"]) == 1
    invoice = payload["invoices"][0]
    assert invoice["status"] == "OVERDUE"
    assert invoice["billing_status"] == "sent"
    assert invoice["billing_status_label"] == "Issued"
    assert invoice["payment_status"] == "pending"
    assert invoice["payment_status_label"] == "Payment pending"
    assert invoice["is_overdue"] is True


def test_fetch_case_invoices_omits_void_invoices(app, monkeypatch):
    with app.app_context():
        monkeypatch.setattr(
            case_invoice_service_module,
            "fetch_case_invoice_ids",
            lambda matter_id: [33],
        )
        monkeypatch.setattr(
            case_invoice_service_module.InvoiceService,
            "get_by_id",
            lambda invoice_id: {
                "id": invoice_id,
                "number": "INV-33",
                "issue_date": "2026-04-01",
                "due_date": "2026-04-30",
                "currency": "USD",
                "total_minor": 630200,
                "billing_status": "void",
                "payment_status": "none",
                "payment_verified": 0,
            },
        )
        monkeypatch.setattr(
            case_invoice_service_module.InvoiceService,
            "get_line_items",
            lambda invoice_id: [],
        )
        monkeypatch.setattr(
            case_invoice_service_module.PaymentService,
            "get_total_paid",
            lambda invoice_id: 0,
        )

        payload = case_invoice_service_module.fetch_case_invoices("M-CASE-VOID")

    assert payload["invoices"] == []
    assert payload["summary"]["total_billed"] == 0
    assert payload["summary"]["outstanding"] == 0
