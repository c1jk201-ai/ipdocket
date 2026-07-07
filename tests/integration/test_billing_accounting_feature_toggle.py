from app.models.system_config import SystemConfig
from app.services.core.config_service import ConfigService


def test_accounting_routes_enabled_by_default_and_runtime_toggle(app, admin_client, db_session):
    from app.blueprints.billing_invoices.db import init_db

    with app.app_context():
        init_db()
        ConfigService.clear_cache()

    response = admin_client.get("/accounting/invoice-system/expenses")
    assert response.status_code == 200

    with app.app_context():
        SystemConfig.set_config("INVOICEAPP_DISABLE_ACCOUNTING_FEATURES", "true")
        db_session.commit()
        ConfigService.clear_cache()

    blocked = admin_client.get("/accounting/invoice-system/expenses")
    assert blocked.status_code == 404
