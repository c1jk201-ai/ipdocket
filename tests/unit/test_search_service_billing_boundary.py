from flask import Flask

from app.services.productivity import search_service


def test_search_invoices_uses_billing_boundary_state(monkeypatch):
    app = Flask(__name__)
    app.config.update(INVOICEAPP_INTEGRATED=True, INVOICEAPP_INIT_OK=False)

    class DummyInvoiceService:
        @staticmethod
        def search_invoices(*, q: str, limit: int):
            assert q == "INV-2026"
            assert limit == 5
            return [
                {
                    "id": 7,
                    "number": "INV-2026-0007",
                    "client_name": "Client A",
                    "issue_date": "2026-03-15",
                    "billing_status": "sent",
                    "payment_status": "unpaid",
                }
            ]

    monkeypatch.setattr(search_service, "InvoiceService", DummyInvoiceService)
    monkeypatch.setattr(search_service, "billing_subsystem_enabled", lambda app: True)
    monkeypatch.setattr(search_service, "billing_subsystem_ready", lambda app: True)
    monkeypatch.setattr(search_service, "_has_permission", lambda _perm: True)

    with app.app_context():
        results = search_service._search_invoices(q="INV-2026", limit=5)

    assert results == [
        {
            "type": "invoice",
            "id": 7,
            "title": "INV-2026-0007",
            "subtitle": "Client A / 2026-03-15 / sent / unpaid",
            "url": "/accounting/invoice-system/invoices/7",
        }
    ]
