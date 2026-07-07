import pytest


def test_invoice_facade_raises_when_billing_services_unavailable(monkeypatch):
    from app.models import invoice as invoice_mod

    monkeypatch.setattr(invoice_mod, "_SERVICES_AVAILABLE", False)
    monkeypatch.setattr(invoice_mod, "_SERVICES_IMPORT_ERROR", ImportError("missing service"))

    with pytest.raises(invoice_mod.InvoiceFacadeUnavailableError):
        invoice_mod.Invoice.get_from_billing(1)


def test_unified_invoice_raises_when_billing_services_unavailable(monkeypatch):
    from app.models import invoice as invoice_mod

    monkeypatch.setattr(invoice_mod, "_SERVICES_AVAILABLE", False)
    monkeypatch.setattr(invoice_mod, "_SERVICES_IMPORT_ERROR", ImportError("missing service"))

    with pytest.raises(invoice_mod.InvoiceFacadeUnavailableError):
        invoice_mod.get_unified_invoice(1)
