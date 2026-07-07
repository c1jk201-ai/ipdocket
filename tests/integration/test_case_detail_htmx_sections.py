from __future__ import annotations

import uuid

import pytest

REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def _create_viewable_case(db_session, sample_user) -> str:
    from app.models.ip_records import Matter, MatterStaffAssignment, VMatterOverview

    sample_user = db_session.merge(sample_user)
    if not (sample_user.staff_party_id or "").strip():
        sample_user.staff_party_id = f"staff_{uuid.uuid4().hex[:8]}"
        db_session.add(sample_user)
        db_session.flush()

    matter_id = uuid.uuid4().hex
    our_ref = f"TEST-HTMX-SECTION-{matter_id[:8]}"
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="HTMX section test",
            right_group="DOM",
            matter_type="PATENT",
            is_deleted=False,
        )
    )
    db_session.add(
        VMatterOverview(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="HTMX section test",
            right_group="DOM",
            matter_type="PATENT",
            applicants="",
            clients="",
            attorneys="",
            entered_at="2026-01-01",
        )
    )
    db_session.add(
        MatterStaffAssignment(
            matter_id=matter_id,
            staff_party_id=sample_user.staff_party_id,
            staff_role_code="attorney",
        )
    )
    db_session.commit()
    return matter_id


def _request_case_section(
    authenticated_client, matter_id: str, section: str, *, htmx: bool = False
):
    headers = {"HX-Request": "true"} if htmx else None
    return authenticated_client.get(
        f"/case/{matter_id}/section/{section}",
        headers=headers,
    )


@pytest.mark.parametrize(
    ("section", "anchor"),
    [
        ("history", "sec-history"),
        ("deadlines", "sec-deadlines"),
        ("memo", "sec-memo"),
    ],
)
def test_case_detail_section_redirects_without_hx(
    authenticated_client,
    sample_user,
    db_session,
    section,
    anchor,
):
    matter_id = _create_viewable_case(db_session, sample_user)

    resp = _request_case_section(authenticated_client, matter_id, section)
    assert resp.status_code in REDIRECT_STATUSES
    location = resp.headers.get("Location", "")
    assert location.endswith(f"/case/{matter_id}#{anchor}")


@pytest.mark.parametrize(
    ("section", "include_text", "exclude_text", "extra_text"),
    [
        ("history", 'id="caseHistoryTable"', 'id="sec-history"', None),
        ("files", 'id="caseFileList"', None, "No registered files."),
        ("deadlines", 'id="caseDeadlineList"', 'id="sec-deadlines"', None),
        ("memo", 'id="caseMemoList"', 'id="sec-memo"', None),
    ],
)
def test_case_detail_section_partial_for_hx(
    authenticated_client,
    sample_user,
    db_session,
    section,
    include_text,
    exclude_text,
    extra_text,
):
    matter_id = _create_viewable_case(db_session, sample_user)

    resp = _request_case_section(authenticated_client, matter_id, section, htmx=True)
    assert resp.status_code == 200

    html = resp.data.decode("utf-8")
    assert include_text in html
    if exclude_text:
        assert exclude_text not in html
    if extra_text:
        assert extra_text in html


def test_case_detail_cost_section_renders_invoice_links_for_hx(
    authenticated_client,
    sample_user,
    db_session,
    monkeypatch,
):
    from app.services.billing.case_finance_service import CaseFinanceService

    matter_id = _create_viewable_case(db_session, sample_user)

    monkeypatch.setattr(
        CaseFinanceService,
        "get_summary",
        staticmethod(
            lambda matter_id, **kwargs: {
                "summary": {
                    "ar": {
                        "billed_minor": 120000,
                        "paid_minor": 50000,
                        "outstanding_minor": 70000,
                        "currency": "USD",
                        "overdue_count": 0,
                    },
                    "ap": {
                        "requested": 0,
                        "paid": 0,
                        "outstanding": 0,
                        "currency": "USD",
                        "unpaid_count": 0,
                    },
                    "links": {"unbilled_expense_count": 0},
                },
                "invoices": [
                    {
                        "invoice_id": 9001,
                        "invoice_no": "INV-9001",
                        "open_url": "/accounting/invoice-system/invoices/9001",
                        "pdf_url": "",
                        "title": "Rendered invoice",
                        "issue_date": "2026-05-01",
                        "due_date": "2026-05-31",
                        "total_minor": 120000,
                        "paid_minor": 50000,
                        "outstanding_minor": 70000,
                        "currency": "USD",
                        "billing_status_label": "Text",
                        "billing_status_pill": "issued",
                        "payment_status": "partial",
                        "payment_status_label": "Text",
                        "payment_status_pill": "partial",
                        "is_overdue": False,
                    }
                ],
                "payables": [],
                "ledger": [],
            }
        ),
    )

    resp = _request_case_section(authenticated_client, matter_id, "cost", htmx=True)

    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert 'id="sec-cost"' in html
    assert "INV-9001" in html
    assert "/accounting/invoice-system/invoices/9001" in html


def test_invoice_module_is_always_active_without_toggle(
    admin_client,
    admin_user,
    db_session,
):
    from app.models.ip_records import MatterCustomField

    matter_id = _create_viewable_case(db_session, admin_user)
    db_session.add(
        MatterCustomField(
            matter_id=matter_id,
            namespace="integrations",
            data={"existing": "kept", "invoice_module_enabled": False},
        )
    )
    db_session.commit()

    resp = _request_case_section(admin_client, matter_id, "cost", htmx=True)

    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "Invoice Module" in html
    assert "Invoice Create" in html
    assert "Existing Invoice Link" in html
    assert "invoiceModuleEnabled" not in html
    assert "/integrations/invoice-module" not in html
    assert "External Invoice  disabled exists." not in html
    rows = MatterCustomField.query.filter_by(matter_id=matter_id, namespace="integrations").all()
    assert len(rows) == 1
    assert rows[0].data["existing"] == "kept"
    assert rows[0].data["invoice_module_enabled"] is False
