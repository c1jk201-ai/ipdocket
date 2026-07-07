from app.blueprints.dashboard import routes as dashboard_routes


def test_dashboard_metrics_rolls_back_after_invoice_summary_failure(admin_client, monkeypatch):
    rollback_calls = []

    monkeypatch.setattr(dashboard_routes, "is_invoice_manager", lambda _user: True)
    monkeypatch.setattr(
        dashboard_routes,
        "invoice_summary",
        lambda: (_ for _ in ()).throw(RuntimeError("invoice summary failed")),
    )
    monkeypatch.setattr(dashboard_routes, "_rollback_session", lambda: rollback_calls.append(True))

    resp = admin_client.get("/business/dashboard/metrics")

    assert resp.status_code == 200
    assert rollback_calls == [True]
    assert resp.get_json()["finance"]["receivables_by_currency"] == {}
