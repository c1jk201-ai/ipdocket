from __future__ import annotations

from app.blueprints.api import routes as api_routes


class _Conn:
    def __init__(
        self,
        *,
        execute_error: Exception | None = None,
        rollback_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.execute_error = execute_error
        self.rollback_error = rollback_error
        self.close_error = close_error
        self.executed: list[tuple[str, tuple]] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def execute(self, sql, params):
        self.executed.append((str(sql), tuple(params)))
        if self.execute_error:
            raise self.execute_error
        return None

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True
        if self.rollback_error:
            raise self.rollback_error

    def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


def test_invoice_open_url_uses_config_base(app):
    with app.app_context():
        app.config["INVOICE_MODULE_VIEW_BASE_URL"] = "/accounting/invoice-system/invoices/"
        assert api_routes._invoice_open_url(123) == "/accounting/invoice-system/invoices/123"


def test_rollback_conn_safely_reports_exception(monkeypatch):
    conn = _Conn(rollback_error=RuntimeError("rollback failed"))
    captured: list[str] = []
    monkeypatch.setattr(
        api_routes,
        "_report_db_operation_error",
        lambda exc, *, context: captured.append(context),
    )

    api_routes._rollback_conn_safely(conn, context="api.test")

    assert conn.rolled_back is True
    assert captured == ["api.test.rollback"]


def test_close_conn_safely_reports_exception(monkeypatch):
    conn = _Conn(close_error=RuntimeError("close failed"))
    captured: list[str] = []
    monkeypatch.setattr(
        api_routes,
        "_report_db_operation_error",
        lambda exc, *, context: captured.append(context),
    )

    api_routes._close_conn_safely(conn, context="api.test")

    assert conn.closed is True
    assert captured == ["api.test.close"]


def test_cleanup_created_invoice_rolls_back_and_closes_on_execute_failure(app):
    conn = _Conn(execute_error=RuntimeError("execute failed"))

    with app.app_context():
        api_routes._cleanup_created_invoice(get_db_fn=lambda: conn, invoice_id=7)

    assert conn.committed is False
    assert conn.rolled_back is True
    assert conn.closed is True
    assert conn.executed == [("DELETE FROM invoices WHERE id=?", (7,))]
