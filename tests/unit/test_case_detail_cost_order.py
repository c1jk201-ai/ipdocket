from __future__ import annotations


def test_build_costs_section_sorts_finance_rows_oldest_first(
    app, db_session, sample_matter, monkeypatch
):
    from app.blueprints.case.services.detail_context import _build_costs_section
    from app.services.billing.case_finance_service import CaseFinanceService

    matter_id = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)

    monkeypatch.setattr(
        CaseFinanceService,
        "get_summary",
        lambda matter_id, **kwargs: {
            "summary": {},
            "invoices": [
                {"invoice_id": 569, "issue_date": "2026-03-25", "title": "new invoice"},
                {"invoice_id": 446, "issue_date": "2026-02-12", "title": "old invoice"},
            ],
            "payables": [
                {"expense_id": "pay-2", "expense_date": "2026-03-20", "description": "new payable"},
                {"expense_id": "pay-1", "expense_date": "2026-02-01", "description": "old payable"},
            ],
            "ledger": [
                {"invoice_id": 569, "date": "2026-03-25", "title": "new ledger", "type": "INVOICE"},
                {"invoice_id": 446, "date": "2026-02-12", "title": "old ledger", "type": "INVOICE"},
            ],
        },
    )

    with app.app_context():
        out = _build_costs_section({"_mid_str": matter_id, "matter": sample_matter})

    assert [row["invoice_id"] for row in out["case_finance_invoices"]] == [446, 569]
    assert [row["expense_id"] for row in out["case_finance_payables"]] == ["pay-1", "pay-2"]
    assert [row["invoice_id"] for row in out["case_finance_ledger"]] == [446, 569]
