from __future__ import annotations

from types import SimpleNamespace


def test_invoice_to_crm_sync_rejects_get(admin_client):
    client = admin_client

    resp = client.get("/crm/clients/sync_from_invoice_clientNewinvoice_client_id=123")

    assert resp.status_code == 405


def test_invoice_to_crm_sync_uses_post(admin_client, monkeypatch):
    client = admin_client
    calls: list[int] = []

    def _fake_sync(invoice_client_id: int):
        calls.append(invoice_client_id)
        return SimpleNamespace(id=456)

    monkeypatch.setattr(
        "app.services.billing.invoice_bridge.ensure_ipm_client_link_from_invoice_client",
        _fake_sync,
    )

    resp = client.post(
        "/crm/clients/sync_from_invoice_client",
        data={
            "invoice_client_id": "123",
            "next": "/accounting/invoice-system/clients",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/accounting/invoice-system/clients")
    assert calls == [123]
