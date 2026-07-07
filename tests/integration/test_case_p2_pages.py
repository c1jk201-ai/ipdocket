def test_status_wizard_page_renders(admin_client, sample_matter):
    matter_id = getattr(sample_matter, "_test_matter_id", str(sample_matter.matter_id))

    response = admin_client.get(f"/case/matter/{matter_id}/status-wizard")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f"/case/matter/{matter_id}/status-wizard" in html
    assert "Status" in html


def test_tc_to_invoice_page_renders(admin_client, sample_matter):
    matter_id = getattr(sample_matter, "_test_matter_id", str(sample_matter.matter_id))

    response = admin_client.get(f"/case/matter/{matter_id}/tc/to-invoice")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Task Log Select" in html
    assert "Invoice" in html
