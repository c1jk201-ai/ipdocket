from urllib.parse import parse_qs, urlparse


def test_accounting_payments_redirects_to_invoice_paid_list(admin_client):
    response = admin_client.get("/accounting/payments", follow_redirects=False)

    assert response.status_code in (302, 303)
    location = response.headers.get("Location") or ""
    parsed = urlparse(location)
    query = parse_qs(parsed.query)

    assert parsed.path.endswith("/accounting/invoice-system/invoices")
    assert query.get("status") == ["paid"]


def test_case_mapping_diag_routes_removed(admin_client):
    assert admin_client.get("/case/dom/patent/mapping").status_code == 404
    assert admin_client.get("/case/pct/mapping").status_code == 404
    assert admin_client.get("/case/litigation/mapping").status_code == 404
