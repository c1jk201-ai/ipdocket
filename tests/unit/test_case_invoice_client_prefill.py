from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from app.models.client import Client
from app.models.matter import MatterCustomField
from app.services.billing import invoice_services as invoice_services_module
from app.services.billing.invoice_prefill import resolve_invoice_create_base_url


def _query_param(url: str, key: str) -> str:
    parsed = urlparse(url)
    return (parse_qs(parsed.query).get(key) or [""])[0]


def _seed_legacy_invoice_basics(app) -> None:
    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) "
            "VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute("INSERT OR REPLACE INTO clients (id, name) VALUES (1, 'Client A')")
        conn.commit()
        conn.close()


def test_select_matter_create_infers_client_id_from_invoice(
    authenticated_client, db_session, monkeypatch
):
    client = Client(name="Text Text")
    db_session.add(client)
    db_session.flush()
    client_id = client.id
    db_session.commit()

    monkeypatch.setattr(
        invoice_services_module.InvoiceService,
        "get_by_id",
        staticmethod(lambda invoice_id: {"id": invoice_id, "client_id": client_id}),
    )

    response = authenticated_client.get("/case/matter/create/selectNewpopup=1&invoice_id=609")

    assert response.status_code == 200
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    link = soup.find(
        "a",
        href=lambda href: href and "division=DOM" in href and "type=PATENT" in href,
    )
    assert link is not None
    assert _query_param(link.get("href") or "", "client_id") == str(client_id)


def test_create_matter_prefills_client_from_invoice(admin_client, db_session, monkeypatch):
    client = Client(name="Text Text Text")
    db_session.add(client)
    db_session.flush()
    client_id = client.id
    client_name = client.name
    db_session.commit()

    monkeypatch.setattr(
        invoice_services_module.InvoiceService,
        "get_by_id",
        staticmethod(lambda invoice_id: {"id": invoice_id, "client_id": client_id}),
    )

    response = admin_client.get("/case/matter/createNewdivision=DOM&type=PATENT&invoice_id=609")

    assert response.status_code == 200
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    client_id_inputs = soup.select(f'input[name="client_id"][value="{client_id}"]')
    client_name_inputs = soup.select(f'input[name="client_name"][value="{client_name}"]')
    applicant_name_inputs = soup.select(f'input[name="applicant_name"][value="{client_name}"]')
    assert client_id_inputs
    assert client_name_inputs
    assert applicant_name_inputs


def test_create_inc_matter_prefills_foreign_agent_but_not_applicant_from_invoice(
    admin_client, db_session, monkeypatch
):
    client = Client(name="Text Text")
    db_session.add(client)
    db_session.flush()
    client_id = client.id
    client_name = client.name
    db_session.commit()

    monkeypatch.setattr(
        invoice_services_module.InvoiceService,
        "get_by_id",
        staticmethod(lambda invoice_id: {"id": invoice_id, "client_id": client_id}),
    )

    response = admin_client.get("/case/matter/createNewdivision=INC&type=PATENT&invoice_id=609")

    assert response.status_code == 200
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    client_id_inputs = soup.select(f'input[name="client_id"][value="{client_id}"]')
    client_name_inputs = soup.select(f'input[name="client_name"][value="{client_name}"]')
    applicant_name_inputs = soup.select(f'input[name="applicant_name"][value="{client_name}"]')
    same_client_checked = soup.select('[data-same-client="1"][checked]')

    assert client_id_inputs
    assert client_name_inputs
    assert not applicant_name_inputs
    assert not same_client_checked


def test_quick_create_invoice_redirect_includes_client_id(admin_client, db_session, sample_matter):
    client = Client(name="Text Text")
    db_session.add(client)
    db_session.flush()
    client_id = client.id

    db_session.add(
        MatterCustomField(
            matter_id=str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id)),
            namespace="basic",
            data={"client_id": str(client_id), "client_name": client.name},
        )
    )
    db_session.commit()

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    matter_ref = str(db_session.get(type(sample_matter), matter_id).our_ref or "")
    response = admin_client.post(f"/case/{matter_id}/quick/invoice")

    assert response.status_code == 302
    location = response.headers.get("Location") or ""
    assert _query_param(location, "ipm_case_id") == matter_id
    assert _query_param(location, "ipm_case_ref") == matter_ref
    assert _query_param(location, "client_id") == str(client_id)


def test_tc_to_invoice_link_includes_client_id(admin_client, db_session, sample_matter):
    client = Client(name="TC Text")
    db_session.add(client)
    db_session.flush()
    client_id = client.id

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    db_session.add(
        MatterCustomField(
            matter_id=matter_id,
            namespace="basic",
            data={"client_id": str(client_id), "client_name": client.name},
        )
    )
    db_session.commit()

    response = admin_client.get(f"/case/matter/{matter_id}/tc/to-invoice")

    assert response.status_code == 200
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    link = soup.find(
        "a",
        href=lambda href: href and "ipm_case_id=" in href,
    )
    assert link is not None
    href = link.get("href") or ""
    assert _query_param(href, "ipm_case_id") == matter_id
    assert _query_param(href, "client_id") == str(client_id)


def test_invoice_mode_toggle_preserves_prefill_params(admin_client, app, clean_legacy_invoice_db):
    _seed_legacy_invoice_basics(app)

    response = admin_client.get(
        "/accounting/invoice-system/invoices/new"
        "Newclient_id=1&ipm_case_id=M-1&ipm_case_ref=REF-1&worklog_ids=11,12"
    )

    assert response.status_code == 200
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    toggle = soup.find("a", id="fxToggleBtn")
    assert toggle is not None
    href = toggle.get("href") or ""
    assert _query_param(href, "outgoing") == "1"
    assert _query_param(href, "client_id") == "1"
    assert _query_param(href, "ipm_case_id") == "M-1"
    assert _query_param(href, "ipm_case_ref") == "REF-1"
    assert _query_param(href, "worklog_ids") == "11,12"


def test_invoice_create_url_prefills_line_items(admin_client, app, clean_legacy_invoice_db):
    _seed_legacy_invoice_basics(app)
    params = urlencode(
        {
            "items": json.dumps(
                [
                    {
                        "description": "Service fee",
                        "item_type": "service",
                        "qty": "1.0",
                        "unit_price": "2000000.0",
                        "discount": "10.0",
                        "is_estimated": "1",
                    },
                    {
                        "description": "Official fee",
                        "item_type": "admin",
                        "qty": "1.0",
                        "unit_price": "46000.0",
                        "discount": "70.0",
                        "is_estimated": "1",
                    },
                ],
                ensure_ascii=False,
            )
        }
    )

    response = admin_client.get(f"/accounting/invoice-system/invoices/newNew{params}")

    assert response.status_code == 200
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    descriptions = [node.get("value") for node in soup.select('input[name="description[]"]')]
    item_types = [
        select.find("option", selected=True).get("value")
        for select in soup.select('select[name="item_type[]"]')
    ]
    qtys = [node.get("value") for node in soup.select('input[name="qty[]"]')]
    unit_prices = [node.get("value") for node in soup.select('input[name="unit_price[]"]')]
    discounts = [node.get("value") for node in soup.select('input[name="discount[]"]')]
    estimated_hidden = [
        node.get("value") for node in soup.select('input[name="is_estimated_base[]"]')
    ]

    assert descriptions == ["Service fee", "Official fee"]
    assert item_types == ["service", "admin"]
    assert qtys == ["1.0", "1.0"]
    assert unit_prices == ["2000000.0", "46000.0"]
    assert discounts == ["10.0", "70.0"]
    assert estimated_hidden == ["1", "1"]


def test_invoice_create_redirect_uses_submitted_outgoing_mode(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_legacy_invoice_basics(app)

    response = admin_client.post(
        "/accounting/invoice-system/invoices/new?outgoing=0",
        data={
            "business_profile_id": "1",
            "client_id": "1",
            "issue_date": "2026-05-01",
            "due_date": "2026-06-01",
            "status": "draft",
            "invoice_language": "en",
            "is_outgoing": "1",
            "description[]": ["Outgoing service"],
            "qty[]": ["1"],
            "unit_price[]": ["1000"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = response.headers.get("Location") or ""
    assert _query_param(location, "outgoing") == "1"

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        row = conn.execute(
            "SELECT is_outgoing FROM invoices ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert int(row["is_outgoing"] or 0) == 1
        conn.close()


def test_new_invoice_form_uses_selected_business_profile_vat_and_currency(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_legacy_invoice_basics(app)

    response = admin_client.get("/accounting/invoice-system/invoices/new")

    assert response.status_code == 200
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    vat_input = soup.find("input", id="vat_rate")
    assert vat_input is not None
    assert vat_input.get("value") == "10.0"

    status_select = soup.find("select", attrs={"name": "status"})
    assert status_select is not None
    assert status_select.find("option", attrs={"value": "paid"}) is None


def test_new_regular_invoice_form_exposes_estimated_line_checkbox(
    admin_client, app, clean_legacy_invoice_db
):
    _seed_legacy_invoice_basics(app)

    response = admin_client.get("/accounting/invoice-system/invoices/new")

    assert response.status_code == 200
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    checkbox = soup.find("input", class_="estimated-checkbox")
    assert checkbox is not None

    controls = checkbox.find_parent("div", class_="phase-controls")
    assert controls is not None
    assert "is-hidden" not in (controls.get("class") or [])

    phase_select = controls.find("select", class_="phase-select")
    assert phase_select is not None
    assert "is-hidden" in (phase_select.get("class") or [])


def test_resolve_invoice_create_base_url_uses_shared_config(monkeypatch, app):
    with app.app_context():
        monkeypatch.setitem(app.config, "INVOICE_MODULE_CREATE_URL", "/billing/custom/new")
        monkeypatch.setitem(app.config, "INVOICE_CREATE_URL", "/billing/legacy/create")
        monkeypatch.setitem(
            app.config,
            "INVOICE_MODULE_VIEW_BASE_URL",
            "/accounting/invoice-system/invoices",
        )
        assert resolve_invoice_create_base_url(config=app.config) == "/billing/custom/new"
