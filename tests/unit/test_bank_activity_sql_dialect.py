from app.blueprints.billing_invoices.routes.bank_activity import (
    _transaction_date_expr,
)


class _FakeConn:
    def __init__(self, dialect_name: str, cursor=None):
        self.dialect_name = dialect_name
        self._cursor = cursor

    def cursor(self):
        if self._cursor is None:
            raise RuntimeError("cursor not configured")
        return self._cursor


def test_transaction_date_expr_postgres_uses_strpos():
    expr = _transaction_date_expr(_FakeConn("postgres"))
    assert "strpos" in expr
    assert "instr" not in expr


def test_transaction_date_expr_sqlite_uses_instr():
    expr = _transaction_date_expr(_FakeConn("sqlite"))
    assert "instr" in expr
    assert "strpos" not in expr
