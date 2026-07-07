def test_business_dashboard_invalid_business_profile_id_does_not_500(admin_client):
    from app.blueprints.billing_invoices.db import init_db

    with admin_client.application.app_context():
        init_db()

    res = admin_client.get("/business/Newbusiness_profile_id=not-an-int")
    assert res.status_code == 200
