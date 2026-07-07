def test_accounting_api_invoices_invalid_issue_date(admin_client, db_session):
    from app.models.case import Case

    c = Case(ref_no="T-CASE-1", title="t")
    db_session.add(c)
    db_session.commit()

    res = admin_client.post(
        "/accounting/api/invoices",
        json={"case_id": c.id, "issue_date": "2024-13-01"},
    )
    assert res.status_code == 400
    assert res.headers["Deprecation"] == "true"
    assert res.headers["X-IPM-Legacy-Compat"] == "accounting-invoice-api"
    assert "/accounting/invoice-system/invoices" in res.headers["Link"]
    data = res.get_json()
    assert data["success"] is False
    assert data["error"]["code"] == "bad_request"


def test_accounting_api_invoices_invalid_total(admin_client, db_session):
    from app.models.case import Case

    c = Case(ref_no="T-CASE-2", title="t")
    db_session.add(c)
    db_session.commit()

    res = admin_client.post(
        "/accounting/api/invoices",
        json={"case_id": c.id, "total": "not-a-number"},
    )
    assert res.status_code == 400
    data = res.get_json()
    assert data["success"] is False
    assert data["error"]["code"] == "bad_request"


def test_accounting_api_invoice_patch_invalid_total(admin_client, db_session):
    from app.models.case import Case
    from app.models.invoice import Invoice

    c = Case(ref_no="T-CASE-3", title="t")
    db_session.add(c)
    db_session.commit()

    inv = Invoice(case_id=c.id, status="draft", currency="USD", total=0)
    db_session.add(inv)
    db_session.commit()

    res = admin_client.patch(
        f"/accounting/api/invoices/{inv.id}",
        json={"total": "not-a-number"},
    )
    assert res.status_code == 400
    data = res.get_json()
    assert data["success"] is False
    assert data["error"]["code"] == "bad_request"
