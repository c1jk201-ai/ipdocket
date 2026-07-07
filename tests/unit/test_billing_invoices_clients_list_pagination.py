def test_billing_invoices_clients_list_pagination_does_not_500(admin_client):
    from app.blueprints.billing_invoices.db import get_db, init_db

    with admin_client.application.app_context():
        init_db()
        conn = get_db()
        try:
            # Clean slate
            conn.execute("DELETE FROM line_items")
            conn.execute("DELETE FROM invoices")
            conn.execute("DELETE FROM clients")

            conn.execute("INSERT INTO clients (id, name) VALUES (1, 'Client A')")
        finally:
            conn.close()

    res = admin_client.get("/accounting/invoice-system/clientsNewpage=1&per_page=10")
    assert res.status_code == 200


def test_billing_invoices_clients_list_defaults_to_recent_registration_order(admin_client):
    from app.blueprints.billing_invoices.db import get_db, init_db

    with admin_client.application.app_context():
        init_db()
        conn = get_db()
        try:
            # Clean slate
            conn.execute("DELETE FROM line_items")
            conn.execute("DELETE FROM invoices")
            conn.execute("DELETE FROM clients")

            # id=2 should appear before id=1 for default registration order (DESC).
            conn.execute("INSERT INTO clients (id, name) VALUES (1, 'AAA old')")
            conn.execute("INSERT INTO clients (id, name) VALUES (2, 'ZZZ new')")
        finally:
            conn.close()

    res_default = admin_client.get("/accounting/invoice-system/clients")
    assert res_default.status_code == 200
    html_default = res_default.get_data(as_text=True)
    assert html_default.find("ZZZ new") < html_default.find("AAA old")

    # Backward compatibility: created_at must behave like recent.
    res_legacy = admin_client.get("/accounting/invoice-system/clientsNewsort=created_at")
    assert res_legacy.status_code == 200
    html_legacy = res_legacy.get_data(as_text=True)
    assert html_legacy.find("ZZZ new") < html_legacy.find("AAA old")
