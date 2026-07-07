from __future__ import annotations

from pathlib import Path


def test_invoice_attachment_analysis_uses_text_nodes_for_untrusted_values():
    template_path = (
        Path(__file__).resolve().parents[2]
        / "app/templates/billing_invoices/partials/invoice_view/_payment_usd.html"
    )
    body = template_path.read_text(encoding="utf-8")

    assert "typeDiv.innerHTML" not in body
    assert "sumDiv.innerHTML" not in body
    assert "d.innerHTML" not in body
    assert "label.textContent" in body
    assert "valueSpan.textContent" in body
