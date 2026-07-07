from app.models.matter import Matter, MatterMemo
from app.models.system_config import SystemConfig
from app.services.core.config_service import ConfigService


def test_invoice_timeline_can_be_enabled_from_system_config(app, db_session, monkeypatch):
    from app.services.billing import invoice_timeline_service as service

    with app.app_context():
        app.config["INVOICE_TIMELINE_TO_CASE_MEMO_ENABLED"] = False
        db_session.add(Matter(matter_id="MID-1", our_ref="MID-1", right_name="Invoice memo matter"))
        SystemConfig.set_config("INVOICE_TIMELINE_TO_CASE_MEMO_ENABLED", "true")
        db_session.commit()
        ConfigService.clear_cache()

        monkeypatch.setattr(service, "_INVOICE_SERVICE_AVAILABLE", True)
        monkeypatch.setattr(service, "_LEGACY_MODELS_AVAILABLE", True)

        class _InvoiceService:
            @staticmethod
            def get_by_id(_invoice_id):
                return {
                    "id": 7,
                    "number": "INV-0007",
                    "currency": "USD",
                    "total": 125000,
                }

        monkeypatch.setattr(service, "InvoiceService", _InvoiceService)
        monkeypatch.setattr(service, "_resolve_matter_ids", lambda *_args, **_kwargs: ["MID-1"])

        service.record_invoice_timeline_event(action="invoice.create", invoice_id=7)

        memos = db_session.query(MatterMemo).all()
        assert len(memos) == 1
        assert memos[0].matter_id == "MID-1"
        assert "[Invoice] Draft" in memos[0].body
        assert "INV-0007" in memos[0].body
        assert "125,000 USD" in memos[0].body
