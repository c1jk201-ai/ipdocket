from __future__ import annotations

from app.blueprints.billing_invoices.routes import invoices as invoice_routes
from app.blueprints.billing_invoices.routes import invoices_logs as invoice_logs_routes


class _Cursor:
    def __init__(self, row) -> None:
        self._row = row

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, *, invoice_row=("", ""), link_row=None) -> None:
        self.invoice_row = invoice_row
        self.link_row = link_row
        self.calls: list[str] = []

    def execute(self, query, params):
        sql = str(query)
        self.calls.append(sql)
        if "SELECT ipm_case_id, ipm_case_ref FROM invoices" in sql:
            return _Cursor(self.invoice_row)
        if "FROM external_invoice_case_map" in sql:
            return _Cursor(self.link_row)
        raise AssertionError(f"Unexpected SQL: {sql}")


def test_normalize_scope_defaults_to_all():
    assert invoice_logs_routes._normalize_scope(None) == invoice_logs_routes.SCOPE_ALL
    assert invoice_logs_routes._normalize_scope("  invalid  ") == invoice_logs_routes.SCOPE_ALL
    assert invoice_logs_routes._normalize_scope(" Billing ") == invoice_logs_routes.SCOPE_BILLING


def test_build_action_filter_clause_for_scopes():
    billing_clause, billing_params = invoice_logs_routes._build_action_filter_clause(
        invoice_logs_routes.SCOPE_BILLING
    )
    assert "a.action IN" in billing_clause
    assert billing_clause.count("?") == len(billing_params)
    assert billing_params == list(
        invoice_logs_routes.SCOPE_ACTIONS[invoice_logs_routes.SCOPE_BILLING]
    )

    all_clause, all_params = invoice_logs_routes._build_action_filter_clause(
        invoice_logs_routes.SCOPE_ALL
    )
    assert all_clause == ""
    assert all_params == []


def test_build_bulk_like_query_adds_mode_filter_for_payment_scope():
    sql, params = invoice_logs_routes._build_bulk_like_query(
        invoice_logs_routes.SCOPE_PAYMENT,
        "INV-INV-1",
    )
    assert "invoice.bulk_status_change" in sql
    assert params == ['%"mode": "payment"%']

    sql_all, params_all = invoice_logs_routes._build_bulk_like_query(
        invoice_logs_routes.SCOPE_ALL,
        "INV-INV-1",
    )
    assert params_all == []
    assert sql_all.count("a.meta LIKE ?") == 0


def test_parse_meta_dict_accepts_legacy_python_repr():
    meta = invoice_logs_routes._parse_meta_dict(
        "{'mode': 'payment', 'invoice_numbers': ['INV-001'], 'new_status': 'paid'}"
    )

    assert meta == {
        "mode": "payment",
        "invoice_numbers": ["INV-001"],
        "new_status": "paid",
    }


def test_build_pretty_meta_supports_legacy_bulk_status_meta():
    pretty = invoice_logs_routes._build_pretty_meta(
        "invoice.bulk_status_change",
        "{'mode': 'payment', 'invoice_numbers': ['INV-001'], 'new_status': 'paid'}",
    )

    assert pretty == "Bulk change - Payment: paid"


def test_bulk_status_change_match_requires_exact_invoice_number():
    meta = {"mode": "payment", "invoice_numbers": ["INV-0011"]}

    assert invoice_routes._matches_bulk_status_change_log(
        meta,
        invoice_id=11,
        invoice_number="INV-0011",
        mode="payment",
    )
    assert not invoice_routes._matches_bulk_status_change_log(
        meta,
        invoice_id=1,
        invoice_number="INV-001",
        mode="payment",
    )


def test_bulk_status_change_match_uses_invoice_ids_when_number_missing():
    meta = {"mode": "billing", "invoice_ids": [7, 8], "new_status": "sent"}

    assert invoice_routes._matches_bulk_status_change_log(
        meta,
        invoice_id=8,
        invoice_number=None,
        mode="billing",
    )
    assert not invoice_routes._matches_bulk_status_change_log(
        meta,
        invoice_id=9,
        invoice_number=None,
        mode="billing",
    )


def test_resolve_case_auto_link_skips_when_primary_link_exists(monkeypatch):
    conn = _Conn(invoice_row=("MATTER-1", "REF-1"), link_row=None)
    monkeypatch.setattr(
        invoice_logs_routes,
        "resolve_matter_identifier",
        lambda *_args, **_kwargs: {"status": "ok", "matter_id": "should-not-be-used"},
    )

    result = invoice_logs_routes._resolve_case_auto_link_for_internal_ref(
        conn,
        invoice_id=1,
        internal_reference="REF-INPUT",
    )

    assert result == {"status": "skipped", "reason": "already_linked"}


def test_resolve_case_auto_link_links_when_unique_match(monkeypatch):
    conn = _Conn(invoice_row=("", ""), link_row=None)
    monkeypatch.setattr(
        invoice_logs_routes,
        "resolve_matter_identifier",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "matter_id": "MATTER-2",
            "our_ref": "OUR-REF-2",
        },
    )
    monkeypatch.setattr(invoice_logs_routes, "link_case_to_invoice", lambda *_args, **_kwargs: True)

    result = invoice_logs_routes._resolve_case_auto_link_for_internal_ref(
        conn,
        invoice_id=2,
        internal_reference="REF-INPUT",
    )

    assert result == {
        "status": "linked",
        "matter_id": "MATTER-2",
        "our_ref": "OUR-REF-2",
        "source": "internal_reference",
    }
